# -*- coding: utf-8 -*-
"""
스크랩 후보 찾기 (scrap-finder)

스크랩마스터에서 이미지로 저장한 하루치 지면 폴더를 OCR로 읽어
병원·의료 관련 기사 후보를 찾고, 결과 화면(HTML)과 목차 초안을 만듭니다.

사용법:  python scrapfinder.py <지면 이미지 폴더>
필요한 것: Python 3.9+, Pillow, Tesseract OCR(한국어 데이터 포함)
"""
import csv
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import median

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit('Pillow가 없습니다. 명령창에서  pip install pillow  를 실행한 뒤 다시 시도하세요.')

# exe로 묶였을 때는 exe가 있는 폴더, 아니면 이 파일이 있는 폴더 (키워드.txt·tesseract 폴더를 여기서 찾음)
HERE = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0   # Windows에서 OCR 돌릴 때 검은 창이 깜빡이지 않게
IMG_EXT = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.webp')
MIN_OCR_WIDTH = 2600          # 이보다 작은 지면은 확대해서 읽음 (작은 글씨 오인식 방지)
HEADLINE_OCR_WIDTH = 900      # 큰 제목을 잡기 위해 줄여서 한 번 더 읽는 폭
HEADER_BAND = 0.06            # 지면 위쪽 6%를 머리글(면·날짜·섹션)로 봄

MEDIA = ['조선일보', '중앙일보', '동아일보', '한국일보', '경향신문', '한겨레', '국민일보', '서울신문',
         '세계일보', '문화일보', '매일경제', '한국경제', '서울경제', '머니투데이', '헤럴드경제',
         '아시아경제', '파이낸셜뉴스', '이데일리', '디지털타임스', '전자신문', '내일신문', '헬스경향', '동아경제']
MEDIA_ALIAS = {'헬스경향': '경향신문', '동아경제': '동아일보'}


# ───────────────────────── 한글 오타 허용 비교 ─────────────────────────
def _jamo(s):
    out = []
    for c in s:
        o = ord(c) - 0xAC00
        if 0 <= o < 11172:
            out += [o // 588, 100 + (o % 588) // 28, 200 + o % 28]
        else:
            out += [c, None, None]
    return out


def jamo_dist(a, b):
    return sum(x != y for x, y in zip(_jamo(a), _jamo(b)))


def find_kw(text, kw, fuzzy):
    """text(공백 제거된 문자열)에서 kw 위치 목록. fuzzy면 자모 2개까지 틀려도 인정."""
    if not fuzzy or len(kw) < 5:
        return [m.start() for m in re.finditer(re.escape(kw), text)]
    L, res, i = len(kw), [], 0
    while i <= len(text) - L:
        if jamo_dist(text[i:i + L], kw) <= 2:
            res.append(i)
            i += L
        else:
            i += 1
    return res


# ───────────────────────── 설정 파일 ─────────────────────────
def load_keywords(path):
    """키워드.txt 읽기.  [분류 이름] 머리 아래 한 줄에 하나.  [분류 min=2] 처럼 쓰면 한 기사에 2개 이상일 때만 후보."""
    cats, cur = [], None
    with open(path, encoding='utf-8-sig') as f:
        for raw in f:
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            m = re.match(r'^\[(.+?)(?:\s+min=(\d+))?\]$', line)
            if m:
                cur = {'name': m.group(1).strip(), 'min': int(m.group(2) or 1), 'kws': []}
                cats.append(cur)
            elif cur is not None:
                cur['kws'].append(line.replace(' ', ''))
    return cats


# ───────────────────────── OCR ─────────────────────────
class SetupError(Exception):
    pass


def find_tesseract():
    bundled = os.path.join(HERE, 'tesseract', 'tesseract.exe')
    if os.path.isfile(bundled):                  # 프로그램에 같이 들어 있는 OCR 엔진
        os.environ['TESSDATA_PREFIX'] = os.path.join(HERE, 'tesseract', 'tessdata')
        return bundled
    cands = [shutil.which('tesseract'),
             r'C:\Program Files\Tesseract-OCR\tesseract.exe',
             r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
             os.path.expandvars(r'%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe')]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise SetupError('OCR 엔진(Tesseract)을 찾지 못했습니다. 프로그램 폴더 안의 tesseract 폴더가 지워지지 않았는지 확인해 주세요.')


def _tesseract_tsv(tess, im, scale, base):
    """im을 scale배로 바꿔 OCR → base.tsv 저장. 이미 있으면 다시 읽지 않음(캐시)."""
    tsv = base + '.tsv'
    if not os.path.exists(tsv):
        tmp = base + '_ocr.png'
        g = im.convert('L')
        if scale != 1.0:
            g = g.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        g.save(tmp)
        env = dict(os.environ, OMP_THREAD_LIMIT='1')   # 여러 면을 동시에 돌릴 때 CPU 경합 방지
        r = subprocess.run([tess, tmp, base, '-l', 'kor', '--psm', '3', 'tsv'],
                           capture_output=True, env=env, creationflags=NO_WINDOW)
        os.remove(tmp)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode('utf-8', 'replace')[-500:])
    words = []
    with open(tsv, encoding='utf-8') as f:
        for row in csv.DictReader(f, delimiter='\t', quoting=csv.QUOTE_NONE):
            if row['level'] != '5' or not (row.get('text') or '').strip():
                continue
            words.append({
                't': row['text'].strip(),
                'x': int(row['left']) / scale, 'y': int(row['top']) / scale,
                'w': int(row['width']) / scale, 'h': int(row['height']) / scale,
                'line': (row['block_num'], row['par_num'], row['line_num']),
            })
    return words


def ocr_page(tess, src, work):
    """한 면을 두 번 읽음.
    ① 본문용: 작은 지면은 확대해서 읽음 (작은 글씨 오인식 방지)
    ② 제목용: 가로 900픽셀로 줄여서 읽음 (Tesseract는 아주 큰 글씨를 건너뛰는 경향이 있음)
    좌표는 모두 원본 이미지 기준으로 돌려줌."""
    key = os.path.splitext(os.path.relpath(src, os.path.dirname(work)))[0]
    base = os.path.join(work, re.sub(r'[\\/:]', '__', key))
    stamp = base + '.ok'
    im = Image.open(src)
    im.load()
    if os.path.exists(stamp) and os.path.getmtime(stamp) < os.path.getmtime(src):
        for ext in ('.tsv', '.big.tsv', '.ok'):
            if os.path.exists(base + ext):
                os.remove(base + ext)
    W, H = im.size
    words = _tesseract_tsv(tess, im, max(1.0, MIN_OCR_WIDTH / W), base)
    big = _tesseract_tsv(tess, im, min(1.0, HEADLINE_OCR_WIDTH / W), base + '.big')
    open(stamp, 'w').close()
    return words, big, (W, H)


def tidy(text):
    """OCR이 '육 아 탓'처럼 글자마다 띄운 경우 붙여서 읽기 좋게."""
    toks = text.split()
    if toks and sum(len(t) for t in toks) / len(toks) < 1.8:
        text = re.sub(r'(?<=[가-힣]) (?=[가-힣])', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def build_lines(words):
    groups = defaultdict(list)
    for w in words:
        groups[w['line']].append(w)
    lines = []
    for ws in groups.values():
        ws.sort(key=lambda w: w['x'])
        x0 = min(w['x'] for w in ws); y0 = min(w['y'] for w in ws)
        x1 = max(w['x'] + w['w'] for w in ws); y1 = max(w['y'] + w['h'] for w in ws)
        lines.append({'words': ws, 'text': tidy(' '.join(w['t'] for w in ws)),
                      'flat': ''.join(w['t'] for w in ws),
                      'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
                      'h': median([w['h'] for w in ws])})
    lines.sort(key=lambda l: (l['y0'], l['x0']))
    return lines


# ───────────────────────── 머리글(매체·면·날짜·섹션) ─────────────────────────
def parse_header(lines, size, path):
    W, H = size
    head = [l for l in lines if l['y1'] < H * HEADER_BAND]
    words = sorted([w for l in head for w in l['words']], key=lambda w: w['x'])
    flat = ''.join(w['t'] for w in words)
    info = {'media': '', 'page': '', 'section': '', 'date': ''}

    hay = flat + ' ' + path.replace(os.sep, ' ')
    for m in MEDIA:
        if m in hay.replace(' ', '') or any(jamo_dist(hay[i:i + len(m)], m) <= 1 for i in range(len(flat) - len(m) + 1)):
            info['media'] = MEDIA_ALIAS.get(m, m)
            info['paper'] = m
            break

    d = re.search(r'(20\d\d)년(\d{1,2})월(\d{1,2})일', flat)
    if d:
        info['date'] = f'{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}'

    for w in (words[:1] + words[-1:]):          # 면 번호는 보통 맨 왼쪽이나 맨 오른쪽 끝
        if re.fullmatch(r'[A-Z]?\d{1,2}', w['t']):
            info['page'] = w['t']
            break
    if not info['page']:                         # 파일 이름에서 A12, B3 같은 패턴
        m = re.search(r'(?<![A-Za-z0-9])([A-Z]\d{1,2})(?!\d)', os.path.basename(path))
        if m:
            info['page'] = m.group(1)

    mid = [w for w in words if abs((w['x'] + w['w'] / 2) - W / 2) < W * 0.15]
    sec = ''.join(w['t'] for w in mid)
    sec = re.sub(r'[^\w·\-&＆ ]', '', sec)
    info['section'] = sec[:12]
    return info


# ───────────────────────── 기사 제목 추정 ─────────────────────────
def headline_for(hit_line, heads):
    """키워드가 나온 줄 위쪽, 같은 단(열)에 걸쳐 있는 가장 가까운 큰 글씨 줄을 제목으로 추정."""
    best, best_d = None, 1e9
    cx = (hit_line['x0'] + hit_line['x1']) / 2
    for h in heads:
        if h['y0'] > hit_line['y0']:
            continue
        if not (h['x0'] - 40 <= cx <= h['x1'] + 40):
            continue
        d = hit_line['y0'] - h['y1']
        if d < best_d:
            best, best_d = h, d
    return best


def head_quality(l):
    """제목 후보 줄의 '한글다움' 점수. 사진·표를 잘못 읽은 잡음(숫자·기호 투성이)은 0."""
    t = re.sub(r'\s', '', l['text'])
    han = len(re.findall(r'[가-힣]', t))
    if han < 4 or han / max(len(t), 1) < 0.6:
        return 0
    return han / len(t) * min(han, 20)


def overlap(a, b):
    ix = max(0, min(a['x1'], b['x1']) - max(a['x0'], b['x0']))
    iy = max(0, min(a['y1'], b['y1']) - max(a['y0'], b['y0']))
    small = min((a['x1'] - a['x0']) * (a['y1'] - a['y0']), (b['x1'] - b['x0']) * (b['y1'] - b['y0'])) or 1
    return ix * iy / small


def merge_heads(heads):
    """잡음을 거르고, 두 번 읽은 같은 제목은 더 잘 읽힌 쪽만 남긴 뒤, 여러 줄로 나뉜 제목을 하나로 묶음."""
    picked = []
    for h in sorted(heads, key=head_quality, reverse=True):
        if head_quality(h) == 0:
            continue
        if any(overlap(h, p) > 0.5 for p in picked):
            continue
        picked.append(h)
    heads = sorted(picked, key=lambda l: (l['y0'], l['x0']))
    out = []
    for h in heads:
        if out:
            p = out[-1]
            if (h['y0'] - p['y1'] < p['h'] * 0.9 and abs(h['h'] - p['h']) < p['h'] * 0.3
                    and h['x0'] < p['x1'] and h['x1'] > p['x0']):
                p['text'] += ' ' + h['text']; p['y1'] = h['y1']
                p['x0'] = min(p['x0'], h['x0']); p['x1'] = max(p['x1'], h['x1'])
                continue
        out.append(dict(h))
    return out


def _junk(tok):
    """사진·숫자를 잘못 읽은 듯한 토막인지. 2.2%→3.9%, KDI, 10만 같은 정상 표기는 제외."""
    if re.fullmatch(r'[\d.,%→~\-·]+[%만억원명건개곳]?|[A-Za-z]+[,.]?|["\'“”‘’?!…,.·\-]+', tok):
        return False
    han = len(re.findall(r'[가-힣]', tok))
    return han / len(tok) < 0.5


def clean_title(text):
    """제목 뒤에 붙은 잡음(사진·숫자를 잘못 읽은 글자)을 잘라 냄."""
    toks = text.split()
    for i in range(len(toks)):                   # 뒤쪽이 잡음 투성이면 그 앞에서 자름
        rest = toks[i:]
        joined = ''.join(rest)
        han = len(re.findall(r'[가-힣]', joined))
        odd = sum(_junk(t) or bool(re.search(r'\d', t)) for t in rest)
        if odd >= 3 and han / max(len(joined), 1) < 0.6:
            toks = toks[:i]
            break
    while toks and _junk(toks[-1]):
        toks.pop()
    t = re.sub(r'[\s^。、ㆍ|~_]+$', '', ' '.join(toks)).strip()
    return t or text.strip()


DOCTOR_RE = re.compile(r'([가-힣]{1,10}(?:과|센터|교실|학교실))([가-힣]{2,4})(교수|원장|센터장|전문의)')


def doctor_note(flat_after):
    m = DOCTOR_RE.match(flat_after)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


# ───────────────────────── 한 면 분석 ─────────────────────────
def analyze(path, words, size, cats, big=()):
    lines = build_lines(words)
    info = parse_header(lines, size, path)
    W, H = size
    body = [l for l in lines if l['y0'] > H * HEADER_BAND]
    mh = median([l['h'] for l in body]) if body else 10
    big_lines = [l for l in build_lines(list(big)) if l['y0'] > H * HEADER_BAND]
    heads = merge_heads([l for l in big_lines + body if l['h'] >= mh * 1.7])

    # 줄 경계를 넘는 키워드도 잡히도록, 단(열)별로 이어 붙인 문자열 + 글자→줄 대응표
    # OCR이 알려 준 읽기 순서(블록→문단→줄)대로 이어야 단을 넘어가는 문장이 섞이지 않음
    body.sort(key=lambda l: tuple(int(v) for v in l['words'][0]['line']))
    stream, owner = '', []
    for i, l in enumerate(body):
        stream += l['flat']; owner += [i] * len(l['flat'])

    articles = {}
    covered = set()                             # '보건복지부' 안의 '복지부'처럼 겹치는 키워드는 한 번만 셈
    for cat in cats:
        for kw in sorted(cat['kws'], key=len, reverse=True):
            for pos in find_kw(stream, kw, fuzzy=True):
                span = set(range(pos, pos + len(kw)))
                if span & covered:
                    continue
                covered |= span
                li = body[owner[pos]]
                hd = headline_for(li, heads)
                key = id(hd) if hd else ('line', owner[pos])
                a = articles.setdefault(key, {'head': hd, 'hits': defaultdict(lambda: defaultdict(int)),
                                              'boxes': [], 'snips': [], 'doctors': []})
                a['hits'][cat['name']][kw] += 1
                a['boxes'].append((li['x0'], li['y0'], li['x1'], li['y1'], cat['name']))
                end = pos + len(kw)
                a['snips'].append(stream[max(0, pos - 12):end + 22])
                if cat is cats[0] or cat['min'] == 1:
                    dn = doctor_note(stream[end:end + 30])
                    if dn:
                        a['doctors'].append((kw, *dn))

    results = []
    for a in articles.values():
        cat_hit = None
        for cat in cats:                        # 위에 적힌 분류가 우선
            n = sum(a['hits'][cat['name']].values()) if cat['name'] in a['hits'] else 0
            if n >= cat['min']:
                cat_hit = cat['name']
                break
        if not cat_hit:
            continue
        title = clean_title(a['head']['text']) if a['head'] else '(제목 추정 실패)'
        results.append({'cat': cat_hit, 'title': title, 'hits': {c: dict(k) for c, k in a['hits'].items()},
                        'boxes': a['boxes'], 'snips': a['snips'][:3], 'doctors': a['doctors'],
                        'head_box': (a['head']['x0'], a['head']['y0'], a['head']['x1'], a['head']['y1']) if a['head'] else None})
    return info, results


# ───────────────────────── 출력 ─────────────────────────
COLORS = [(255, 90, 0, 95), (0, 120, 255, 80), (240, 200, 0, 110), (0, 170, 90, 80)]


def draw_page(src, results, cats, out):
    im = Image.open(src).convert('RGB')
    dr = ImageDraw.Draw(im, 'RGBA')
    cidx = {c['name']: i for i, c in enumerate(cats)}
    for r in results:
        if r['head_box']:
            x0, y0, x1, y1 = r['head_box']
            dr.rectangle([x0 - 4, y0 - 4, x1 + 4, y1 + 4], outline=(220, 0, 0, 255), width=4)
        for x0, y0, x1, y1, c in r['boxes']:
            dr.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], fill=COLORS[cidx[c] % len(COLORS)])
    im.save(out, quality=80)


def toc_line(page, r):
    src = f"{page['media'] or '매체?'} {page['section'] or ''}면".replace(' 면', '면')
    s = f"- {r['title']} ({src})"
    seen = set()
    for kw, dept, name, role in r['doctors']:
        k = (dept, name)
        if k in seen:
            continue
        seen.add(k)
        s += f"\n  : {kw} {dept} {name} {role} 도움말"
    return s


def write_outputs(folder, pages, cats, outdir):
    date = next((p['info']['date'] for p in pages if p['info']['date']), time.strftime('%Y-%m-%d'))
    order = [c['name'] for c in cats]
    by_cat = defaultdict(list)
    for p in pages:
        for r in p['results']:
            by_cat[r['cat']].append((p, r))

    # 목차 초안 (지금 스크랩 1쪽 형식)
    y, m, d = date.split('-')
    wd = '월화수목금토일'[time.strptime(date, '%Y-%m-%d').tm_wday]
    toc = [f'{int(m)}월 {int(d)}일({wd}) 언론 스크랩 [자동 초안 — 반드시 확인 후 수정]', '']
    n = 0
    for c in order:
        if not by_cat[c]:
            continue
        n += 1
        toc.append(f'{n}. {c} 관련')
        for p, r in by_cat[c]:
            toc.append(toc_line(p['info'], r))
        toc.append('')
    with open(os.path.join(outdir, '목차초안.txt'), 'w', encoding='utf-8-sig') as f:
        f.write('\n'.join(toc))

    # 결과 화면
    def esc(s):
        return html.escape(str(s))
    rows = []
    for c in order:
        items = by_cat[c]
        rows.append(f'<h2>{esc(c)} <small>{len(items)}건</small></h2>')
        if not items:
            rows.append('<p class="none">후보 없음</p>')
            continue
        rows.append('<ol class="items">')
        for p, r in items:
            i = p['info']
            hits = ', '.join(f'{esc(k)}×{v}' for kk in r['hits'].values() for k, v in kk.items())
            docs = ''.join(f'<div class="doc">: {esc(kw)} {esc(dp)} {esc(nm)} {esc(rl)} 도움말</div>'
                           for kw, dp, nm, rl in dict.fromkeys(r['doctors']))
            snips = ''.join(f'<div class="snip">…{esc(s)}…</div>' for s in r['snips'])
            rows.append(
                f'<li><a href="#{esc(p["id"])}" class="where">{esc(i["media"] or "매체?")} '
                f'<b>{esc(i["page"] or "?")}면</b> {esc(i["section"])}</a>'
                f'<div class="title">{esc(r["title"])}</div>{docs}'
                f'<div class="hits">{hits}</div>{snips}</li>')
        rows.append('</ol>')
    pages_html = []
    for p in pages:
        if not p['results']:
            continue
        i = p['info']
        pages_html.append(f'<figure id="{esc(p["id"])}"><figcaption>{esc(i["media"])} {esc(i["page"])}면 '
                          f'{esc(i["section"])} — <span>{esc(os.path.basename(p["src"]))}</span></figcaption>'
                          f'<img loading="lazy" src="면/{esc(p["id"])}.jpg"></figure>')
    skipped = [os.path.basename(p['src']) for p in pages if not p['results']]
    legend = ''.join(f'<span class="lg" style="background:rgba{COLORS[k % len(COLORS)][:3] + (0.45,)}">{esc(c)}</span>'
                     for k, c in enumerate(order))
    doc = f'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>스크랩 후보 {esc(date)}</title>
<style>
:root{{--bg:#fff;--fg:#1d2330;--mut:#667;--line:#e3e6ec;--acc:#0b5ed7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#15181e;--fg:#e8eaee;--mut:#99a;--line:#2b303a;--acc:#6ea8fe}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 "Malgun Gothic","Apple SD Gothic Neo",sans-serif}}
main{{display:grid;grid-template-columns:minmax(320px,440px) 1fr;gap:0;height:100vh}}
#list{{overflow:auto;padding:20px;border-right:1px solid var(--line)}}
#pages{{overflow:auto;padding:20px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:22px 0 8px;border-bottom:2px solid var(--fg);padding-bottom:4px}}
h2 small{{color:var(--mut);font-weight:400}}
.meta{{color:var(--mut);font-size:13px}} .lg{{display:inline-block;padding:0 6px;margin-right:4px;border-radius:3px;color:#000}}
.items{{padding-left:20px}} .items li{{margin:0 0 14px}}
.where{{color:var(--acc);text-decoration:none;font-size:13px}} .title{{font-weight:700}}
.doc{{color:#c45000;font-size:13px}} .hits{{font-size:12px;color:var(--mut)}}
.snip{{font-size:12px;color:var(--mut);word-break:break-all}} .none{{color:var(--mut)}}
figure{{margin:0 0 28px}} figcaption{{font-weight:700;margin-bottom:6px}} figcaption span{{font-weight:400;color:var(--mut);font-size:12px}}
figure img{{width:100%;max-width:1100px;border:1px solid var(--line)}}
@media (max-width:800px){{main{{display:block;height:auto}}#list{{border:0}}}}
</style>
<main><section id="list"><h1>스크랩 후보 · {esc(date)}</h1>
<div class="meta">지면 {len(pages)}면 확인 · 후보가 있는 면 {len(pages) - len(skipped)}면<br>
형광펜: {legend} · 빨간 테두리 = 추정 제목<br>
목차 초안: <a href="목차초안.txt">목차초안.txt</a></div>
{''.join(rows)}
<details><summary class="meta">후보 없는 면 {len(skipped)}개</summary><div class="meta">{esc(', '.join(skipped))}</div></details>
</section><section id="pages">{''.join(pages_html) or '<p class="none">후보가 있는 면이 없습니다.</p>'}</section></main></html>'''
    with open(os.path.join(outdir, '결과.html'), 'w', encoding='utf-8') as f:
        f.write(doc)
    with open(os.path.join(outdir, '결과.json'), 'w', encoding='utf-8') as f:
        json.dump([{'file': os.path.basename(p['src']), **p['info'],
                    'articles': [{k: v for k, v in r.items() if k not in ('boxes', 'head_box')} for r in p['results']]}
                   for p in pages], f, ensure_ascii=False, indent=1)
    return os.path.join(outdir, '결과.html')


def list_images(folder):
    return sorted(os.path.join(dp, f) for dp, _, fs in os.walk(folder) for f in fs
                  if f.lower().endswith(IMG_EXT) and '_스크랩후보' not in dp)


def run(folder, log=print, progress=None, stop=None):
    """폴더 하나를 처리하고 결과.html 경로를 돌려줌.
    log(문자열): 진행 메시지 / progress(끝난 수, 전체 수) / stop(): True를 돌려주면 중단"""
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise SetupError(f'폴더가 아닙니다: {folder}')
    srcs = list_images(folder)
    if not srcs:
        raise SetupError(f'지면 이미지가 없습니다: {folder}\n(jpg, png, tif, webp 등을 넣어 주세요)')
    kw_path = os.path.join(HERE, '키워드.txt')
    if not os.path.isfile(kw_path):
        raise SetupError(f'키워드.txt가 없습니다: {kw_path}')
    cats = load_keywords(kw_path)
    tess = find_tesseract()
    outdir = os.path.join(folder, '_스크랩후보')
    work = os.path.join(outdir, '.ocr')
    os.makedirs(os.path.join(outdir, '면'), exist_ok=True)
    os.makedirs(work, exist_ok=True)

    log(f'지면 {len(srcs)}면을 읽습니다. (처음 한 번은 면당 10초 안팎, 다시 돌리면 바로 끝납니다)')
    t0 = time.time()
    pages, done = [None] * len(srcs), 0
    if progress:
        progress(0, len(srcs))
    with ThreadPoolExecutor(max_workers=max(1, (os.cpu_count() or 2))) as ex:
        futs = {ex.submit(ocr_page, tess, s, work): k for k, s in enumerate(srcs)}
        for fu in as_completed(futs):
            if stop and stop():
                for f in futs:
                    f.cancel()
                raise SetupError('중단했습니다. (이미 읽은 면은 저장돼 있어 다시 돌리면 이어서 빠르게 끝납니다)')
            k = futs[fu]
            try:
                words, big, size = fu.result()
            except Exception as e:
                log(f'  ! {os.path.basename(srcs[k])} 읽기 실패: {e}')
                words, big, size = [], [], Image.open(srcs[k]).size
            info, results = analyze(srcs[k], words, size, cats, big)
            pid = f'p{k + 1:03d}'
            pages[k] = {'id': pid, 'src': srcs[k], 'info': info, 'results': results}
            if results:
                draw_page(srcs[k], results, cats, os.path.join(outdir, '면', pid + '.jpg'))
            done += 1
            mark = f'후보 {len(results)}건' if results else ''
            log(f'  [{done}/{len(srcs)}] {os.path.relpath(srcs[k], folder)}  {info["media"]} {info["page"]} {mark}')
            if progress:
                progress(done, len(srcs))

    out = write_outputs(folder, pages, cats, outdir)
    total = sum(len(p['results']) for p in pages)
    log(f'\n완료 ({time.time() - t0:.0f}초) · 후보 기사 {total}건')
    log(f'결과 화면: {out}')
    return out


def main():
    for stream in (sys.stdout, sys.stderr):      # 한글을 못 찍는 명령창에서도 멈추지 않게
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass
    if len(sys.argv) >= 2:
        folder = sys.argv[1]
    else:
        folder = input('지면 이미지 폴더를 이 창에 끌어다 놓거나 경로를 붙여넣고 Enter: ')
    try:
        out = run(folder.strip().strip('"'))
    except SetupError as e:
        sys.exit(str(e))
    if os.name == 'nt':
        os.startfile(out)


if __name__ == '__main__':
    main()
