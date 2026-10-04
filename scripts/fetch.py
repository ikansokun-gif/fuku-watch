#!/usr/bin/env python3
"""服セールウォッチ: fetch shop data and product photos (runs on GitHub Actions).

1. Photos: want.json (written by Claude's morning run) lists the photo URL of every
   in-stock product. Each one is saved, small, to thumbs/<photo id>.jpg.
2. Data (best effort): for every active Shopify collection in targets.json, reads the
   shop's public product data (<collection>/products.json) into data/<target id>.json.
   Some shops refuse requests from GitHub's servers (HTTP 429); then the file records
   the error and Claude reads the shop's pages itself instead.
Claude's morning run reads these files and puts them on the 服セールウォッチ page.
"""
import datetime
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from PIL import Image

UA = 'fuku-watch/1.0 (personal price tracker, once a day)'
JST = datetime.timezone(datetime.timedelta(hours=9))
RECENT_SOLD_DAYS = 60   # keep sold-out products published within this many days
THUMB_WIDTH = 360
THUMB_QUALITY = 72
PAGE_SIZE = 250
MAX_PAGES = 40


def get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403, 404):
                break
            if e.code == 429:  # the shop says slow down: wait as asked, at most once
                if i > 0:
                    break
                try:
                    wait = int(e.headers.get('Retry-After') or 30)
                except ValueError:
                    wait = 30
                time.sleep(min(max(wait, 10), 60))
                continue
        except Exception as e:  # network hiccup
            last = e
        time.sleep(3 * (i + 1))
    raise RuntimeError(f'{url}: {last}')


def photo_id(tid, key):
    """Same rule as the page: target id + product path, unsafe characters -> '_'."""
    return re.sub(r'[^A-Za-z0-9_\-.~:@+]', '_', f'{tid}--{key}')[:190]


def is_shopify(url):
    return '/collections/' in urlparse(url).path


def fetch_collection(url):
    base = url.split('?')[0].split('#')[0].rstrip('/')
    products = []
    for page in range(1, MAX_PAGES + 1):
        data = json.loads(get(f'{base}/products.json?limit={PAGE_SIZE}&page={page}'))
        batch = data.get('products', [])
        products += batch
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(1.5)
    return products


def summarize(p):
    variants = p.get('variants') or []
    avail = [v for v in variants if v.get('available')]
    pool = avail or variants
    prices = [float(v['price']) for v in pool if v.get('price') not in (None, '')]
    compare = [float(v['compare_at_price']) for v in variants if v.get('compare_at_price') not in (None, '')]
    images = p.get('images') or []
    sizes = []
    for v in avail:
        label = v.get('option2') or v.get('title') or ''
        if label and label not in sizes:
            sizes.append(label)
    return {
        'title': p.get('title') or '',
        'vendor': p.get('vendor') or '',
        'price': int(min(prices)) if prices else None,
        'compare_at': int(max(compare)) if compare else None,
        'available': bool(avail),
        'sizes': sizes[:12],
        'image': images[0].get('src') if images else None,
        'published_at': p.get('published_at') or '',
    }


def public_host(url):
    """Only fetch photos from ordinary public hostnames (the URLs come from shop pages)."""
    host = (urlparse(url).hostname or '').lower()
    if not host or '.' not in host or host == 'localhost' or host.endswith(('.local', '.internal')):
        return False
    return not re.fullmatch(r'[0-9.]+|\[?[0-9a-f:]+\]?', host)


def save_thumb(src, path):
    if not public_host(src):
        raise RuntimeError('refused host')
    raw = get(src + ('&' if '?' in src else '?') + 'width=720')
    if len(raw) > 15_000_000:
        raise RuntimeError('image too large')
    im = Image.open(io.BytesIO(raw))
    if im.width * im.height > 50_000_000:
        raise RuntimeError('image too large')
    if im.mode not in ('RGB', 'L'):
        bg = Image.new('RGB', im.size, (255, 255, 255))
        bg.paste(im.convert('RGBA'), mask=im.convert('RGBA').split()[-1])
        im = bg
    im = im.convert('RGB')
    if im.width > THUMB_WIDTH:
        im = im.resize((THUMB_WIDTH, round(im.height * THUMB_WIDTH / im.width)), Image.LANCZOS)
    im.save(path, 'JPEG', quality=THUMB_QUALITY, optimize=True, progressive=True)


def main():
    targets = json.load(open('targets.json', encoding='utf-8')).get('targets', [])
    now = datetime.datetime.now(JST)
    cutoff = (now - datetime.timedelta(days=RECENT_SOLD_DAYS)).isoformat()
    os.makedirs('data', exist_ok=True)
    os.makedirs('thumbs', exist_ok=True)
    index_path = 'thumbs/index.json'
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    keep, processed, report = set(), [], []
    want = json.load(open('want.json', encoding='utf-8')) if os.path.exists('want.json') else {}
    want = want.get('photos', want) if isinstance(want, dict) else {}

    for t in targets:
        tid, url = t.get('id'), t.get('url', '')
        if not tid or t.get('active') is False or not is_shopify(url):
            continue
        processed.append(tid)
        if t.get('fetch_data') is not True:  # off unless turned on for a shop that allows it
            continue
        record = {'target_id': tid, 'url': url, 'brand': t.get('brand'),
                  'fetched_at': now.isoformat(timespec='seconds'), 'products': {}}
        try:
            products = fetch_collection(url)
        except Exception as e:
            record['error'] = str(e)[:300]
            json.dump(record, open(f'data/{tid}.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
            report.append(f'{tid}: ERROR {e}')
            # keep this target's existing photos
            keep.update(k for k in index if k.startswith(photo_id(tid, '')))
            continue
        for p in products:
            s = summarize(p)
            if not s['available'] and s['published_at'] < cutoff:
                continue
            record['products']['/products/' + p['handle']] = s
        for key, s in record['products'].items():
            if s['available'] and s['image']:
                want.setdefault(photo_id(tid, key), s['image'])
        json.dump(record, open(f'data/{tid}.json', 'w', encoding='utf-8'),
                  ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        report.append(f'{tid}: {len(products)} products, kept {len(record["products"])}')

    new_photos = failed = 0
    for pid, src in sorted(want.items()):
        if not isinstance(src, str) or not src:
            continue
        src = 'https:' + src if src.startswith('//') else src
        if not src.startswith('https://'):
            continue
        keep.add(pid)
        path = f'thumbs/{pid}.jpg'
        if os.path.exists(path) and index.get(pid) == src:
            continue
        try:
            save_thumb(src, path)
            index[pid] = src
            new_photos += 1
            time.sleep(0.2)
        except Exception as e:
            failed += 1
            print(f'photo failed {pid}: {e}', file=sys.stderr)
    report.append(f'photos wanted {len(want)}, new {new_photos}, failed {failed}')

    # drop photos nobody wants any more (no longer in stock, or target removed)
    for pid in list(index):
        if pid not in keep:
            index.pop(pid, None)
            try:
                os.remove(f'thumbs/{pid}.jpg')
            except FileNotFoundError:
                pass
    json.dump(index, open(index_path, 'w'), ensure_ascii=False, indent=0, sort_keys=True)
    print('\n'.join(report) or 'no Shopify targets')


if __name__ == '__main__':
    main()
