#!/usr/bin/env python3
"""服セールウォッチ: fetch shop data and product photos (runs on GitHub Actions).

Reads targets.json, and for every active Shopify collection:
  - reads the shop's public product data (<collection>/products.json)
  - writes data/<target id>.json   (in-stock products + products that sold out recently)
  - saves a small photo of every in-stock product to thumbs/<photo id>.jpg
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


def save_thumb(src, path):
    raw = get(src + ('&' if '?' in src else '?') + 'width=720')
    im = Image.open(io.BytesIO(raw))
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

    for t in targets:
        tid, url = t.get('id'), t.get('url', '')
        if not tid or t.get('active') is False or not is_shopify(url):
            continue
        processed.append(tid)
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
        new_photos = failed = 0
        for key, s in record['products'].items():
            if not s['available'] or not s['image']:
                continue
            pid = photo_id(tid, key)
            keep.add(pid)
            path = f'thumbs/{pid}.jpg'
            if os.path.exists(path) and index.get(pid) == s['image']:
                continue
            try:
                save_thumb(s['image'], path)
                index[pid] = s['image']
                new_photos += 1
                time.sleep(0.3)
            except Exception as e:
                failed += 1
                print(f'photo failed {key}: {e}', file=sys.stderr)
        json.dump(record, open(f'data/{tid}.json', 'w', encoding='utf-8'),
                  ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        report.append(f'{tid}: {len(products)} products, kept {len(record["products"])}, '
                      f'new photos {new_photos}, failed {failed}')

    # drop photos of products that are no longer in stock (only for targets processed this run)
    prefixes = tuple(photo_id(tid, '') for tid in processed)
    for pid in list(index):
        if pid.startswith(prefixes) and pid not in keep:
            index.pop(pid, None)
            try:
                os.remove(f'thumbs/{pid}.jpg')
            except FileNotFoundError:
                pass
    json.dump(index, open(index_path, 'w'), ensure_ascii=False, indent=0, sort_keys=True)
    print('\n'.join(report) or 'no Shopify targets')


if __name__ == '__main__':
    main()
