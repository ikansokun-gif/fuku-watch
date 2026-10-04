#!/usr/bin/env python3
"""服セールウォッチ: daily diff engine.

Work dir layout (all under ./fw):
  targets/<tid>.json      targets collection (ArtifactData list out_dir)
  catalog/<tid>.json      catalog collection (ArtifactData list out_dir)
  feed/<YYYY-MM>.json     current month feed doc, if it exists
  meta/status.json        meta/status doc, if it exists
  scan/<tid>.txt          first pages, every product: "path|price|S or I|title" (or "ERROR=<reason>")
  scan/<tid>.deep.txt     later pages on full scans, in-stock products only
  verify.json             {"<tid>|<key>": {"brand": str, "price": int, "available": bool, "image": str, "sizes": [str]} or null}
Commands:
  python3 fw.py pages <YYYY-MM-DD>   -> which targets to scan and how
  python3 fw.py diff  <YYYY-MM-DD>   -> fw/diff.json + products to verify (drop/new/restock/image)
  python3 fw.py apply <YYYY-MM-DD> <HH:MM>  -> fw/out/* docs to write + fw/out/summary.txt
  python3 fw.py want <repo dir>      -> writes <repo>/want.json and <repo>/targets.json for the GitHub photo job
  python3 fw.py thumbs <repo dir>    -> photo docs for the page from <repo>/thumbs/*.jpg (fw/out/thumbs, batches to write)
"""
import base64, datetime, glob, hashlib, json, os, re, sys
from urllib.parse import urlparse

FW = 'fw'
MAX_VERIFY = 25
MAX_IMAGE_BACKFILL = 10
PRUNE_DAYS = 120


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
        path = urlparse(path).path
    i = path.find('/products/')
    if i >= 0:
        path = path[i:]
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
        path, price, stock, title = [x.strip() for x in line.split('|', 3)]
        digits = re.sub(r'[^0-9]', '', price)
        k = key_of(path)
        if not digits or k in ('/', '/products'):
            continue
        rows[k] = {'price': int(digits), 'in_stock': stock.upper()[:1] != 'S', 'title': title, 'deep': deep}
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
        error = 'no products found on the page'
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


def is_full(t, cat, date):
    if not cat or not cat.get('baseline_done'):
        return True
    if int(t.get('daily_pages', 2) or 0) == 0:
        return True
    return datetime.date.fromisoformat(date).weekday() == 6  # Sunday


def cmd_pages(date):
    targets, cats = docs('targets'), docs('catalog')
    plan = []
    for tid, t in targets.items():
        if t.get('active') is False:
            continue
        cat = cats.get(tid)
        full = is_full(t, cat, date)
        plan.append({'tid': tid, 'brand': t.get('brand'), 'site': t.get('site'), 'url': t['url'],
                     'platform': platform(t['url']), 'mode': 'full' if full else 'daily',
                     'daily_pages': int(t.get('daily_pages', 2) or 2) if not full else None,
                     'all_rows_pages': int(t.get('daily_pages', 2) or 2) or 2,
                     'baseline': not (cat and cat.get('baseline_done'))})
    dump(os.path.join(FW, 'pages.json'), plan)
    for p in plan:
        if p['mode'] == 'full':
            how = f"ALL pages (pages 1-{p['all_rows_pages']}: every product; later pages: in-stock only)"
        else:
            how = f"pages 1-{p['daily_pages']} only (every product)"
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
        for kind in ('drop', 'new', 'restock', 'image'):
            for k in r[kind]:
                if len(todo) < MAX_VERIFY + (MAX_IMAGE_BACKFILL if kind == 'image' else 0):
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
    now_iso = f'{date}T{hhmm}:00+09:00'
    new_events, errors, written = [], [], []
    seen_ev = {(e['date'], e['type'], e['target_id'], e['key']) for e in feed['events']}

    def add_event(ev):
        sig = (ev['date'], ev['type'], ev['target_id'], ev['key'])
        if sig not in seen_ev:
            seen_ev.add(sig)
            feed['events'].append(ev)
            new_events.append(ev)

    for tid, p in plan.items():
        t = targets.get(tid, {})
        cat = cats.get(tid) or {'items': {}}
        cat.update({'target_id': tid, 'site': t.get('site'), 'brand': t.get('brand'),
                    'url': t.get('url'), 'origin': origin(t.get('url', ''))})
        items = cat.setdefault('items', {})
        rows, err = parse_scan(tid)
        cat['last_checked'] = now_iso
        if err and not rows:
            cat['status'], cat['status_msg'] = 'error', f'{date} 読み込めませんでした（{err}）'
            errors.append({'target_id': tid, 'brand': t.get('brand'), 'msg': err})
            dump(os.path.join(FW, 'out', 'catalog', f'{tid}.json'), cat)
            written.append(f'catalog/{tid}')
            continue
        baseline = not cat.get('baseline_done')
        full = p['mode'] == 'full'
        prev_stock = [k for k, it in items.items() if it.get('in_stock')]
        base_ev = {'date': date, 'target_id': tid, 'site': t.get('site'), 'brand': t.get('brand')}
        want_brand = norm(t.get('brand'))
        for k, row in rows.items():
            v = verified.get(f'{tid}|{k}')
            v = v if isinstance(v, dict) else None
            vprice = v.get('price') if v and isinstance(v.get('price'), (int, float)) and v.get('price') > 0 else None
            vstock = v.get('available') if v and isinstance(v.get('available'), bool) else None
            vimg = img_url(v.get('image')) if v else None
            vsizes = [str(z)[:20] for z in v['sizes'][:12]] if v and isinstance(v.get('sizes'), list) else None
            it = items.get(k)
            if not it:
                if v and v.get('brand') and want_brand and want_brand not in norm(v['brand']) and norm(v['brand']) not in want_brand:
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
                if not baseline and stock:
                    add_event({**base_ev, 'type': 'new', 'key': k, 'title': row['title'],
                               'url': cat['origin'] + k, 'image': vimg, 'price': price, 'old_price': None,
                               'list_price': price, 'in_stock': True})
                continue
            it['title'] = row['title'] or it.get('title')
            it['last_seen'] = date
            if vimg:
                it['image'] = vimg
            if vsizes is not None:
                it['sizes'] = vsizes
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
                if new < old and not baseline:
                    add_event({**base_ev, 'type': 'drop', 'key': k, 'title': it['title'],
                               'url': cat['origin'] + k, 'image': it.get('image'), 'price': new, 'old_price': old,
                               'list_price': it['list_price'], 'in_stock': it['in_stock']})
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
    tlist = [{'id': tid, 'site': t.get('site'), 'brand': t.get('brand'), 'url': t.get('url'),
              'active': t.get('active', True) is not False, 'fetch_data': False}
             for tid, t in sorted(targets.items())]
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


if __name__ == '__main__':
    cmd, args = sys.argv[1], sys.argv[2:]
    {'pages': cmd_pages, 'diff': cmd_diff, 'apply': cmd_apply, 'want': cmd_want, 'thumbs': cmd_thumbs}[cmd](*args)
