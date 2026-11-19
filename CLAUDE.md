# amc-press-scrap

홍보팀이 스크랩마스터에서 이미지로 저장한 하루치 신문 지면을 OCR(Tesseract 한국어)로 읽어
병원·의료 관련 기사 후보와 스크랩 1쪽 목차 초안을 만드는 Windows 프로그램입니다. 사용법은 README.md.

- `scrapfinder.py` 핵심 처리, `app.py` 창 화면(tkinter). exe는 GitHub Actions(`.github/workflows/build.yml`)가 Windows에서 PyInstaller로 만들고 Tesseract·한국어 데이터를 함께 묶습니다.
- 배포: main에 올린 뒤 `v*` 태그를 push하면 Releases에 `스크랩후보찾기.zip`이 올라갑니다.
- 확인: `python build/smoke_test.py . <한글 글꼴.ttf>` (빌드 서버에서도 같은 시험을 돌립니다).
- 다음 단계로 '드래그로 오리기 → 스크랩 PDF 출력' 기능을 같은 프로그램에 추가할 예정입니다.

## 작업 규칙

- **지면 이미지·결과물은 절대 커밋하지 않습니다.** 신문 저작권 때문입니다. 시험용 지면은 저장소 밖에 두세요.
- 팀원이 메모장으로 고치는 파일은 `키워드.txt`뿐입니다. 형식을 바꾸면 README와 파일 머리 설명도 같이 고치세요.
- 실행 환경은 팀 Windows PC입니다(Python + Pillow + Tesseract, 인터넷 없이 동작). 새 의존성은 꼭 필요할 때만 추가하고 README 설치 순서에 반영하세요.
- 정확도를 바꾸는 수정은 실제 스크랩에 들어간 기사를 찾는지로 확인합니다. 예) 2026-11-19 경향신문 H01면 「뜨거운 물 샤워」 → `: 서울아산병원 피부과 원종현 교수 도움말`
- 커밋 메시지는 한글 서술형 한 줄입니다.
