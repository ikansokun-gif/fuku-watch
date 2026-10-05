#!/usr/bin/env python3
"""服セールウォッチ: daily diff engine.

Work dir layout (all under ./fw):
  targets/<tid>.json      watch targets = one brand on one site (ArtifactData list out_dir)
  sites/<sid>.json        sites: {name, url, kind: "new"|"used", point_rate}
  brands/<bid>.json       brands: {name, aliases: [old names]}
  catalog/<tid>.json      catalog collection (ArtifactData list out_dir)
  feed/<YYYY-MM>.json     current month feed doc, if it exists
  meta/status.json        meta/status doc, if it exists
  scan/<tid>.txt          first K pages, in-stock products: "path|price|gender|title" (or "ERROR=<reason>")
                          gender: M men / W women / U unisex / - unknown
                          (older formats "path|price|S or I|title" and "path|price|I|gender|title" also read)
  scan/<tid>.deep.txt     pages after K on full scans, same format
  verify.json             {"<tid>|<key>": {"brand": str, "price": int, "available": bool, "image": str, "sizes": [str]} or null}
Commands:
  python3 fw.py pages <YYYY-MM-DD>   -> which targets to scan and how
  python3 fw.py diff  <YYYY-MM-DD>   -> fw/diff.json + products to verify (drop/new/restock/image)
  python3 fw.py apply <YYYY-MM-DD> <HH:MM>  -> fw/out/* docs to write + fw/out/summary.txt
  python3 fw.py want <repo dir>      -> writes <repo>/want.json and <repo>/targets.json for the GitHub photo job
  python3 fw.py thumbs <repo dir>    -> photo docs for the page from <repo>/thumbs/*.jpg (fw/out/thumbs, batches to write)
  python3 fw.py discover <YYYY-MM-DD>        -> pages to read to find each brand on each site (fw/discover/plan.json)
  python3 fw.py discover-apply <YYYY-MM-DD>  -> new check targets (fw/out/targets, also copied to fw/targets)
  python3 fw.py schedule                     -> run times the owner chose on the page (meta/settings) -> cron
"""
import base64, datetime, glob, hashlib, json, os, re, sys
from urllib.parse import urlparse, quote

FW = 'fw'
MAX_VERIFY = 12          # page reads for checking (price drops only): keeps AI usage low
MAX_IMAGE_BACKFILL = 0   # photos are off
VERIFY_KINDS = ('drop',)
GENDERS = ('M', 'W', 'U', '-')
PRUNE_DAYS = 120
DISCOVER_RETRY_DAYS = 30
MAX_SEARCH_PER_RUN = 30
TOP_STALE_DAYS = 14   # search-style targets: not seen for this long -> treat as gone


def load(p, default=None):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def dump(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))


def docs(sub):
    out = {}
    for fp in sorted(glob.glob(os.path.join(FW, sub, '*.json'))):
        d = load(fp)
        if isinstance(d, dict):
            out[os.path.splitext(os.path.basename(fp))[0]] = d
    return out


def platform(url):
    return 'shopify' if '/collections/' in urlparse(url).path else 'generic'


def origin(url):
    u = urlparse(url)
    return f'{u.scheme}://{u.netloc}'


def key_of(path):
    path = path.strip()
    if path.startswith('http'):
        u = urlparse(path)
        path = u.path + (('?' + u.query) if u.query else '')
    i = path.find('/products/')
    if i >= 0:
        path = path[i:]
    m = re.match(r'^/?\??((?:pid|id|item_id)=\d+)', path)   # ColorMeShop style: /?pid=123 (or /pid=123)
    if m:
        return '/?' + m.group(1)
    path = path.split('?')[0].split('#')[0].rstrip('/')
    return path if path.startswith('/') else '/' + path


def parse_lines(fp, deep):
    rows, error = {}, None
    if not os.path.exists(fp):
        return rows, None
    for line in open(fp, encoding='utf-8'):
        line = line.strip()
        if line.startswith('ERROR='):
            error = line[6:].strip() or 'error'
            continue
        if not line or '=' in line.split('|', 1)[0] or line.count('|') < 3:
            continue
        parts = [x.strip() for x in line.split('|')]
        path, price = parts[0], parts[1]
        third = parts[2].upper()
        if len(parts) >= 5 and third[:1] in ('I', 'S') and parts[3].upper() in GENDERS:
            stock, gender, title = third, parts[3].upper(), '|'.join(parts[4:])
        elif third in GENDERS:
            stock, gender, title = 'I', third, '|'.join(parts[3:])
        else:
            stock, gender, title = third, '-', '|'.join(parts[3:])
        digits = re.sub(r'[^0-9]', '', price)
        k = key_of(path)
        if not digits or k in ('/', '/products'):
            continue
        rows[k] = {'price': int(digits), 'in_stock': stock[:1] != 'S', 'title': title.strip(),
                   'deep': deep, 'gender': gender if gender in ('M', 'W', 'U') else None}
    return rows, error


def parse_scan(tid):
    base = os.path.join(FW, 'scan', tid)
    if not os.path.exists(base + '.txt'):
        return None, 'not scanned'
    rows, err = parse_lines(base + '.txt', False)
    deep, err2 = parse_lines(base + '.deep.txt', True)
    for k, r in deep.items():
        rows.setdefault(k, r)
    error = err or err2
    if not rows and not error:
        error = 'no in-stock products found (all sold out, or the page could not be read)'
    return rows, error


def norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def img_url(u):
    if not isinstance(u, str):
        return None
    u = u.strip()
    if u.startswith('//'):
        u = 'https:' + u
    return u if re.match(r'^https://[^\s"<>]+$', u) else None


def verify_url(t, key):
    if platform(t.get('url', '')) == 'shopify':
        return origin(t['url']) + key + '.oembed'
    return origin(t['url']) + key


def resolve(t, sites=None, brands=None):
    """Names and settings of a target, from its site and brand records (falls back to the target's own fields)."""
    sites = sites if sites is not None else docs('sites')
    brands = brands if brands is not None else docs('brands')
    s = sites.get(t.get('site_id') or '', {})
    b = brands.get(t.get('brand_id') or '', {})
    aliases = [a for a in (b.get('aliases') or []) if isinstance(a, str) and a.strip()]
    return {'site': s.get('name') or t.get('site') or '', 'brand': b.get('name') or t.get('brand') or '',
            'aliases': aliases, 'kind': s.get('kind') if s.get('kind') in ('new', 'used') else 'new',
            'site_id': t.get('site_id'), 'brand_id': t.get('brand_id')}


def brand_matches(vendor, names):
    v = norm(vendor)
    if not v:
        return True
    for n in names:
        n = norm(n)
        if n and (n in v or v in n):
            return True
    return False


def is_full(t, cat, date):
    if t.get('scan') == 'top':
        return False
    if not cat or not cat.get('baseline_done'):
        return False  # first look: daily pages only; the first full scan adds deeper stock quietly
    if int(t.get('daily_pages', 2) or 0) == 0:
        return True
    return datetime.date.fromisoformat(date).weekday() == 6  # Sunday


def cmd_pages(date, only=None):
    targets, cats = docs('targets'), docs('catalog')
    sites, brands = docs('sites'), docs('brands')
    only = set(only.split(',')) if only else None   # one-off full scan of these site ids
    plan = []
    for tid, t in targets.items():
        if t.get('active') is False or not t.get('url'):
            continue
        cat = cats.get(tid)
        full = is_full(t, cat, date)
        r = resolve(t, sites, brands)
        if only is not None:
            if r['site_id'] not in only:
                continue
            full = True
        plan.append({'tid': tid, 'brand': r['brand'], 'site': r['site'], 'url': t['url'],
                     'platform': platform(t['url']), 'mode': 'full' if full else 'daily',
                     'daily_pages': (int(t.get('daily_pages', 2) or 0) or 3) if not full else None,
                     'all_rows_pages': int(t.get('daily_pages', 2) or 2) or 2,
                     'baseline': not (cat and cat.get('baseline_done'))})
    dump(os.path.join(FW, 'pages.json'), plan)
    for p in plan:
        if p['mode'] == 'full':
            how = f"ALL pages (K={p['all_rows_pages']}: pages 1-{p['all_rows_pages']} -> scan/{p['tid']}.txt, later pages -> scan/{p['tid']}.deep.txt)"
        else:
            how = f"pages 1-{p['daily_pages']} only (K={p['daily_pages']}) -> scan/{p['tid']}.txt"
        print(f"{p['tid']}\t{p['brand']} @ {p['site']}\t{p['platform']}\t{p['url']}\t{how}"
              + ("\tBASELINE (no notifications)" if p['baseline'] else ''))
    if not plan:
        print('NO ACTIVE TARGETS')


def cmd_diff(date):
    targets, cats = docs('targets'), docs('catalog')
    plan = {p['tid']: p for p in load(os.path.join(FW, 'pages.json'), [])}
    result, todo = {}, []
    for tid, p in plan.items():
        t = targets.get(tid, {})
        rows, err = parse_scan(tid)
        cat = cats.get(tid) or {}
        items = cat.get('items', {})
        baseline = not cat.get('baseline_done')
        r = {'error': err, 'rows': len(rows or {}), 'drop': [], 'new': [], 'restock': []}
        for k, row in (rows or {}).items():
            it = items.get(k)
            if it and row['price'] < it.get('price', 0):
                r['drop'].append(k)
            elif not it and row['in_stock'] and not baseline:
                r['new'].append(k)
            elif it and row['deep'] and row['in_stock'] and not it.get('in_stock'):
                r['restock'].append(k)
        queued = set(r['drop'] + r['new'] + r['restock'])
        r['image'] = [k for k, it in items.items()
                      if it.get('in_stock') and not it.get('image') and k not in queued][:MAX_IMAGE_BACKFILL]
        result[tid] = r
        for kind in VERIFY_KINDS:
            for k in r[kind]:
                if len(todo) < MAX_VERIFY:
                    title = rows[k]['title'] if rows and k in rows else items[k].get('title', '')
                    todo.append(f"{tid}|{k}\t{verify_url(t, k)}\t{kind}\t{title}")
    dump(os.path.join(FW, 'diff.json'), result)
    for tid, r in result.items():
        print(f"{tid}: rows={r['rows']} drop={len(r['drop'])} new={len(r['new'])} restock={len(r['restock'])} image={len(r['image'])}"
              + (f" ERROR={r['error']}" if r['error'] else ''))
    print(f'VERIFY {len(todo)}:' if todo else 'VERIFY: none')
    for line in todo:
        print(line)


def cmd_apply(date, hhmm):
    targets, cats = docs('targets'), docs('catalog')
    plan = {p['tid']: p for p in load(os.path.join(FW, 'pages.json'), [])}
    verified = load(os.path.join(FW, 'verify.json'), {}) or {}
    month = date[:7]
    feed = load(os.path.join(FW, 'feed', f'{month}.json')) or {'month': month, 'events': []}
    status = load(os.path.join(FW, 'meta', 'status.json')) or {}
    cutoff = (datetime.date.fromisoformat(date) - datetime.timedelta(days=PRUNE_DAYS)).isoformat()
    _h = str(hhmm).replace(':', '').zfill(4)
    now_iso = f'{date}T{_h[:2]}:{_h[2:4]}:00+09:00'
    new_events, errors, written = [], [], []
    seen_ev = {(e['date'], e['type'], e['target_id'], e['key']) for e in feed['events']}

    def add_event(ev):
        sig = (ev['date'], ev['type'], ev['target_id'], ev['key'])
        if sig not in seen_ev:
            seen_ev.add(sig)
            feed['events'].append(ev)
            new_events.append(ev)

    sites, brands = docs('sites'), docs('brands')
    for tid, p in plan.items():
        t = targets.get(tid, {})
        rs = resolve(t, sites, brands)
        cat = cats.get(tid) or {'items': {}}
        cat.update({'target_id': tid, 'site': rs['site'], 'brand': rs['brand'], 'site_id': rs['site_id'],
                    'brand_id': rs['brand_id'], 'kind': rs['kind'],
                    'url': t.get('url'), 'origin': origin(t.get('url', ''))})
        items = cat.setdefault('items', {})
        rows, err = parse_scan(tid)
        cat['last_checked'] = now_iso
        if err and not rows:
            cat['status'], cat['status_msg'] = 'error', f'{date} 読み込めませんでした（{err}）'
            errors.append({'target_id': tid, 'brand': rs['brand'], 'msg': err})
            dump(os.path.join(FW, 'out', 'catalog', f'{tid}.json'), cat)
            written.append(f'catalog/{tid}')
            continue
        baseline = not cat.get('baseline_done')
        full = p['mode'] == 'full'
        prev_stock = [k for k, it in items.items() if it.get('in_stock')]
        base_ev = {'date': date, 'target_id': tid, 'site': rs['site'], 'brand': rs['brand'], 'kind': rs['kind']}
        brand_names = [rs['brand']] + rs['aliases']
        for k, row in rows.items():
            v = verified.get(f'{tid}|{k}')
            v = v if isinstance(v, dict) else None
            vprice = v.get('price') if v and isinstance(v.get('price'), (int, float)) and v.get('price') > 0 else None
            vstock = v.get('available') if v and isinstance(v.get('available'), bool) else None
            vimg = img_url(v.get('image')) if v else None
            vsizes = [str(z)[:20] for z in v['sizes'][:12]] if v and isinstance(v.get('sizes'), list) else None
            it = items.get(k)
            if not it:
                if v and v.get('brand') and not brand_matches(v['brand'], brand_names):
                    continue  # another brand's product picked up from the page
                price = int(vprice or row['price'])
                stock = row['in_stock'] if vstock is None else vstock
                items[k] = {'title': row['title'], 'price': price, 'list_price': price,
                            'in_stock': stock, 'first_seen': date, 'last_seen': date,
                            'history': [[date, price]]}
                if vimg:
                    items[k]['image'] = vimg
                if vsizes is not None:
                    items[k]['sizes'] = vsizes
                if row.get('gender'):
                    items[k]['gender'] = row['gender']
                if not baseline and stock and not (row['deep'] and cat.get('quiet_deep')):
                    add_event({**base_ev, 'type': 'new', 'key': k, 'title': row['title'],
                               'url': cat['origin'] + k, 'image': vimg, 'gender': row.get('gender'),
                               'price': price, 'old_price': None, 'list_price': price, 'in_stock': True})
                continue
            it['title'] = row['title'] or it.get('title')
            it['last_seen'] = date
            if vimg:
                it['image'] = vimg
            if vsizes is not None:
                it['sizes'] = vsizes
            if row.get('gender'):
                it['gender'] = row['gender']
            if row['deep'] and row['in_stock'] and not it.get('in_stock'):
                it['in_stock'] = bool(vstock)  # deep pages may miss sold-out badges: trust only a verified restock
            else:
                it['in_stock'] = row['in_stock'] if vstock is None else vstock
            old = it.get('price', 0)
            new = int(vprice) if (vprice and row['price'] < old) else row['price']
            if new != old:
                it['price'] = new
                it.setdefault('history', []).append([date, new])
                it['list_price'] = max(it.get('list_price', 0), old, new)
                if new < old and not baseline and it['in_stock']:  # only tell about markdowns you can buy
                    add_event({**base_ev, 'type': 'drop', 'key': k, 'title': it['title'],
                               'url': cat['origin'] + k, 'image': it.get('image'), 'gender': it.get('gender'),
                               'price': new, 'old_price': old, 'list_price': it['list_price'], 'in_stock': True})
        for vk, vv in verified.items():  # photo backfill for items not on today's pages
            if vk.startswith(tid + '|') and isinstance(vv, dict):
                k = vk[len(tid) + 1:]
                if k in items and k not in rows and img_url(vv.get('image')):
                    items[k]['image'] = img_url(vv['image'])
        msg = ''
        if full:
            seen = sum(1 for k in prev_stock if k in rows)
            if len(prev_stock) >= 10 and seen < 0.5 * len(prev_stock):
                msg = f'{date} 全ページ確認で見つかった商品が少なかったため、売り切れの判定を見送りました'
            else:
                for k in prev_stock:
                    if k not in rows:
                        items[k]['in_stock'] = False
            cat['last_full'] = date
            cat['quiet_deep'] = False
        if t.get('scan') == 'top':
            stale = (datetime.date.fromisoformat(date) - datetime.timedelta(days=TOP_STALE_DAYS)).isoformat()
            for it in items.values():
                if it.get('in_stock') and it.get('last_seen', date) < stale:
                    it['in_stock'] = False
        if baseline and t.get('scan') != 'top':
            cat['quiet_deep'] = True
        for k in [k for k, it in items.items() if not it.get('in_stock') and it.get('last_seen', date) < cutoff]:
            del items[k]
        cat['baseline_done'] = True
        cat['status'] = 'ok'
        cat['status_msg'] = msg or (f'{date} 初回の読み込みが終わりました（{len(items)} 点）' if baseline else '')
        if err:
            cat['status_msg'] = (cat['status_msg'] + ' ' if cat['status_msg'] else '') + f'一部のページを読み込めませんでした（{err}）'
        dump(os.path.join(FW, 'out', 'catalog', f'{tid}.json'), cat)
        written.append(f'catalog/{tid}')

    n_new = sum(1 for e in new_events if e['type'] == 'new')
    n_drop = sum(1 for e in new_events if e['type'] == 'drop')
    if new_events:
        dump(os.path.join(FW, 'out', 'feed', f'{month}.json'), feed)
        written.append(f'feed/{month}')
    run = {'date': date, 'finished_at': now_iso, 'new_count': n_new, 'drop_count': n_drop,
           'targets': len(plan), 'errors': errors}
    status['last_run'] = run
    status['runs'] = ([run] + [r for r in status.get('runs', []) if r.get('date') != date])[:30]
    dump(os.path.join(FW, 'out', 'meta', 'status.json'), status)
    written.append('meta/status')

    lines = []
    if n_drop or n_new:
        lines.append(f'値下げ {n_drop}件・新着 {n_new}件')
    else:
        lines.append('今日は新着も値下げもありませんでした')
    for e in sorted(new_events, key=lambda e: (e['type'] != 'drop', e['brand'] or '')):
        name = re.sub(r'\s*\[[^\]]+\]\s*$', '', e['title'] or '')
        if e['type'] == 'drop':
            off = round((1 - e['price'] / e['list_price']) * 100) if e.get('list_price') else 0
            lines.append(f"・値下げ{f' -{off}%' if off else ''} {e['brand']} {name} ¥{e['old_price']:,}→¥{e['price']:,}")
        else:
            lines.append(f"・新着 {e['brand']} {name} ¥{e['price']:,}")
        if len(lines) > 12:
            lines.append(f'…ほか {len(new_events) - 12} 件')
            break
    for er in errors:
        lines.append(f"※ {er['brand']} の一覧を読み込めませんでした（{er['msg']}）")
    with open(os.path.join(FW, 'out', 'summary.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('WRITE:', ' '.join(written))
    print('\n'.join(lines))


def photo_id(tid, key):
    return re.sub(r'[^A-Za-z0-9_\-.~:@+]', '_', f'{tid}--{key}')[:190]


def current_catalogs():
    cats = docs('catalog')
    cats.update(docs(os.path.join('out', 'catalog')))
    return cats


def cmd_want(repo):
    targets, cats = docs('targets'), current_catalogs()
    want = {}
    for tid, cat in cats.items():
        if tid not in targets or targets[tid].get('active') is False:
            continue
        for k, it in cat.get('items', {}).items():
            img = img_url(it.get('image'))
            if it.get('in_stock') and img:
                want[photo_id(tid, k)] = img
    sites, brands = docs('sites'), docs('brands')
    tlist = []
    for tid, t in sorted(targets.items()):
        r = resolve(t, sites, brands)
        tlist.append({'id': tid, 'site': r['site'], 'brand': r['brand'], 'kind': r['kind'], 'url': t.get('url'),
                      'active': t.get('active', True) is not False, 'fetch_data': False})
    changed = []
    for name, obj in (('want.json', {'photos': want}), ('targets.json', {'targets': tlist})):
        path = os.path.join(repo, name)
        new = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + '\n'
        old = open(path, encoding='utf-8').read() if os.path.exists(path) else None
        if old is None or json.loads(old) != obj:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new)
            changed.append(name)
    missing = [pid for pid in want if not os.path.exists(os.path.join(repo, 'thumbs', pid + '.jpg'))]
    print(f'photos wanted {len(want)}, not yet in repo {len(missing)}')
    print('CHANGED:', ' '.join(changed) if changed else 'none')


def cmd_thumbs(repo):
    cats, out_cats = current_catalogs(), docs(os.path.join('out', 'catalog'))
    batches, cur, size, touched = [], [], 0, set()
    for tid, cat in sorted(cats.items()):
        for k, it in sorted(cat.get('items', {}).items()):
            fp = os.path.join(repo, 'thumbs', photo_id(tid, k) + '.jpg')
            if not os.path.exists(fp):
                continue
            raw = open(fp, 'rb').read()
            did = 'p' + hashlib.sha1(f'{tid}|{k}|'.encode() + raw).hexdigest()[:16]
            if it.get('thumb') == did:
                continue
            src = 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()
            p = os.path.abspath(os.path.join(FW, 'out', 'thumbs', did + '.json'))
            dump(p, {'src': src, 'target_id': tid, 'key': k})
            it['thumb'] = did
            touched.add(tid)
            if cur and (len(cur) >= 50 or size + len(src) > 800_000):
                batches.append(cur)
                cur, size = [], 0
            cur.append({'op': 'set', 'collection': 'thumbs', 'doc_id': did, 'file_path': p})
            size += len(src) + 300
    if cur:
        batches.append(cur)
    for tid in touched:
        dump(os.path.join(FW, 'out', 'catalog', f'{tid}.json'), cats[tid])
    extra = [f'catalog/{tid}' for tid in sorted(touched) if tid not in out_cats]
    dump(os.path.join(FW, 'out', 'thumb_batches.json'), batches)
    print(f'new photo docs {sum(len(b) for b in batches)} in {len(batches)} batches')
    if extra:
        print('ALSO WRITE:', ' '.join(extra))
    for i, b in enumerate(batches, 1):
        print(f'BATCH {i}: ' + json.dumps(b, ensure_ascii=False))


def brand_names(b):
    return [n for n in [b.get('name')] + list(b.get('aliases') or []) if isinstance(n, str) and n.strip()]


def safe_id(x):
    return re.sub(r'[^A-Za-z0-9_\-.~:@+]', '_', x)[:150]


def cmd_discover(date, only=None):
    only = set(only.split(',')) if only else None   # re-check these site ids now, ignoring the retry wait
    sites, brands, targets = docs('sites'), docs('brands'), docs('targets')
    rec = (load(os.path.join(FW, 'meta', 'discovery.json')) or {}).get('pairs', {})
    cutoff = (datetime.date.fromisoformat(date) - datetime.timedelta(days=DISCOVER_RETRY_DAYS)).isoformat()
    have = {(t.get('site_id'), t.get('brand_id')) for t in targets.values()}
    plan, searches = [], 0
    for sid, st in sorted(sites.items()):
        idx = (st.get('brand_index') or '').strip()
        tpl = (st.get('search_url') or '').strip()
        if st.get('auto') is False or st.get('auto_active') is False or not (idx or '{q}' in tpl):
            continue
        if only is not None and sid not in only:
            continue
        pend = []
        for bid, b in sorted(brands.items()):
            if (sid, bid) in have:
                continue
            r = rec.get(f'{sid}|{bid}')
            if only is None and r and r.get('date', '') > cutoff and r.get('names') == brand_names(b):
                continue
            pend.append(bid)
        if not pend:
            continue
        if '{q}' in tpl:
            for bid in pend:
                if searches >= MAX_SEARCH_PER_RUN:
                    break
                searches += 1
                plan.append({'how': 'search', 'sid': sid, 'bids': [bid],
                             'url': tpl.replace('{q}', quote(brands[bid]['name'])),
                             'file': f'{safe_id(sid)}--{safe_id(bid)}.txt'})
        else:
            plan.append({'how': 'index', 'sid': sid, 'bids': pend, 'url': idx, 'file': f'{safe_id(sid)}.txt'})
    dump(os.path.join(FW, 'discover', 'plan.json'), plan)
    if not plan:
        print('DISCOVER: none')
    for p in plan:
        if p['how'] == 'index':
            names = '; '.join(f"{bid}={' / '.join(brand_names(brands[bid]))}" for bid in p['bids'])
            print(f"INDEX\t{p['url']}\tsave to fw/discover/{p['file']}\tbrands: {names}")
        else:
            print(f"SEARCH\t{p['url']}\tsave to fw/discover/{p['file']}")


def cmd_discover_apply(date):
    sites, brands, targets = docs('sites'), docs('brands'), docs('targets')
    plan = load(os.path.join(FW, 'discover', 'plan.json'), []) or []
    disc = load(os.path.join(FW, 'meta', 'discovery.json')) or {}
    pairs = disc.setdefault('pairs', {})
    have_urls = {t.get('url') for t in targets.values()}
    created, written = [], []

    def make(sid, bid, url, scan):
        st, b = sites.get(sid, {}), brands.get(bid, {})
        tid = f'ta-{safe_id(sid)}-{safe_id(bid)}'[:190]
        doc = {'site_id': sid, 'brand_id': bid, 'site': st.get('name', ''), 'brand': b.get('name', ''), 'url': url,
               'daily_pages': 1, 'active': st.get('auto_active', True) is not False, 'auto': True, 'scan': scan,
               'created_at': f'{date}T00:00:00+09:00'}
        dump(os.path.join(FW, 'out', 'targets', f'{tid}.json'), doc)
        dump(os.path.join(FW, 'targets', f'{tid}.json'), doc)
        have_urls.add(url)
        created.append(f"{b.get('name')} @ {st.get('name')}" + ('' if doc['active'] else '（一時停止で作成）'))
        written.append(f'targets/{tid}')

    for p in plan:
        sid = p['sid']
        fp = os.path.join(FW, 'discover', p['file'])
        if not os.path.exists(fp):
            continue  # not read this time; try again next run
        text = open(fp, encoding='utf-8').read()
        if p['how'] == 'index':
            found = {}
            for line in text.splitlines():
                if '|' not in line:
                    continue
                bid, url = [x.strip() for x in line.split('|', 1)]
                if bid in p['bids'] and url.startswith('http') and url not in have_urls:
                    found[bid] = url.split('#')[0]
            for bid in p['bids']:
                if bid in found:
                    make(sid, bid, found[bid], 'list')
                pairs[f'{sid}|{bid}'] = {'date': date, 'found': bid in found, 'names': brand_names(brands.get(bid, {}))}
        else:
            bid = p['bids'][0]
            rows, err = parse_lines(fp, False)
            names = brand_names(brands.get(bid, {}))
            hit = any(any(norm(n) and norm(n) in norm(r['title']) for n in names) for r in rows.values())
            if hit and p['url'] not in have_urls:
                make(sid, bid, p['url'], 'top')
            pairs[f'{sid}|{bid}'] = {'date': date, 'found': bool(hit), 'names': names}
    dump(os.path.join(FW, 'out', 'meta', 'discovery.json'), disc)
    written.append('meta/discovery')
    print('NEW TARGETS:', ', '.join(created) if created else 'none')
    print('WRITE:', ' '.join(written))


def cmd_schedule():
    settings = load(os.path.join(FW, 'meta', 'settings.json')) or {}
    hours = sorted({int(h) for h in settings.get('hours', []) if str(h).isdigit() and 0 <= int(h) <= 23})
    minute = settings.get('minute')
    if not hours or not isinstance(minute, int) or not 0 <= minute <= 59:
        print('SCHEDULE: unchanged')
        return
    cron = f"CRON_TZ=Asia/Tokyo {minute} {','.join(map(str, hours))} * * *"
    status_path = os.path.join(FW, 'out', 'meta', 'status.json')
    status = load(status_path) or load(os.path.join(FW, 'meta', 'status.json')) or {}
    if status.get('cron') == cron:
        print('SCHEDULE: unchanged')
        return
    status['cron'] = cron
    status['times'] = [f'{h}:{minute:02d}' for h in hours]
    dump(status_path, status)
    print('SCHEDULE: APPLY', cron)


if __name__ == '__main__':
    cmd, args = sys.argv[1], sys.argv[2:]
    {'pages': cmd_pages, 'diff': cmd_diff, 'apply': cmd_apply, 'want': cmd_want, 'thumbs': cmd_thumbs,
     'discover': cmd_discover, 'discover-apply': cmd_discover_apply, 'schedule': cmd_schedule}[cmd](*args)
