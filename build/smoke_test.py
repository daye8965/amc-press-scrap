# -*- coding: utf-8 -*-
"""빌드 확인용: 가짜 지면 이미지를 그려서 묶인 OCR 엔진으로 서울아산병원 기사를 찾는지 확인.
사용법: python smoke_test.py <scrapfinder.py와 tesseract 폴더가 있는 폴더> <글꼴 경로>"""
import os
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

app_dir, font_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, app_dir)
import scrapfinder as sf  # noqa: E402

big = ImageFont.truetype(font_path, 90)
body = ImageFont.truetype(font_path, 34)
im = Image.new('L', (2600, 3600), 255)
d = ImageDraw.Draw(im)
d.text((80, 40), 'A12          사회          2026년 11월 19일 목요일 시험일보', font=body, fill=0)
d.text((80, 300), '겨울철 피부 건조 주의보', font=big, fill=0)
lines = ['찬바람이 매서워질수록 뜨거운 물로 오래 씻는 습관이',
         '피부를 더 건조하게 만든다. 서울아산병원 피부과 홍길동',
         '교수는 "미지근한 물로 짧게 씻는 것이 좋다"고 말했다.',
         '보습제는 씻은 직후 바르는 것이 효과적이다.']
for i, t in enumerate(lines):
    d.text((80, 520 + i * 60), t, font=body, fill=0)

tmp = tempfile.mkdtemp()
im.save(os.path.join(tmp, '시험지면_A12.png'))
out = sf.run(tmp)
toc = open(os.path.join(tmp, '_스크랩후보', '목차초안.txt'), encoding='utf-8-sig').read()
print(toc)
assert '1. 서울아산병원 관련' in toc, '서울아산병원 기사를 찾지 못함'
assert '피부과 홍길동 교수 도움말' in toc, '도움말 줄을 만들지 못함'
print('SMOKE TEST OK')
