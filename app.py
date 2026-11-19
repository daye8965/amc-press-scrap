# -*- coding: utf-8 -*-
"""스크랩 후보 찾기 — 창 화면. exe로 묶어 팀 PC에 배포합니다."""
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import scrapfinder as sf

APP_NAME = '스크랩 후보 찾기'
SETTINGS = os.path.join(sf.HERE, '설정.json')


def load_settings():
    try:
        with open(SETTINGS, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        with open(SETTINGS, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def open_path(p):
    if os.name == 'nt':
        os.startfile(p)


class App:
    def __init__(self, root, start_folder=None):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self.stop_flag = False
        self.result = None
        self.settings = load_settings()

        root.title(APP_NAME)
        root.geometry('760x520')
        root.minsize(560, 400)
        try:
            ttk.Style().theme_use('vista' if os.name == 'nt' else 'clam')
        except tk.TclError:
            pass
        font = ('Malgun Gothic', 10)
        root.option_add('*Font', font)

        frm = ttk.Frame(root, padding=16)
        frm.pack(fill='both', expand=True)

        ttk.Label(frm, text='스크랩마스터에서 저장한 하루치 지면 이미지 폴더', font=('Malgun Gothic', 10, 'bold')).pack(anchor='w')
        row = ttk.Frame(frm)
        row.pack(fill='x', pady=(6, 12))
        self.folder = tk.StringVar(value=start_folder or self.settings.get('last_folder', ''))
        ttk.Entry(row, textvariable=self.folder).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='폴더 선택…', command=self.pick).pack(side='left', padx=(8, 0))

        btns = ttk.Frame(frm)
        btns.pack(fill='x')
        self.btn_run = ttk.Button(btns, text='▶  후보 찾기', command=self.start)
        self.btn_run.pack(side='left')
        self.btn_stop = ttk.Button(btns, text='중단', command=self.stop, state='disabled')
        self.btn_stop.pack(side='left', padx=(8, 0))
        self.btn_open = ttk.Button(btns, text='결과 열기', command=self.open_result, state='disabled')
        self.btn_open.pack(side='left', padx=(8, 0))
        ttk.Button(btns, text='키워드 편집', command=self.edit_keywords).pack(side='right')

        self.bar = ttk.Progressbar(frm, mode='determinate')
        self.bar.pack(fill='x', pady=(14, 4))
        self.status = tk.StringVar(value='폴더를 고르고 「후보 찾기」를 누르세요.')
        ttk.Label(frm, textvariable=self.status).pack(anchor='w')

        box = ttk.Frame(frm)
        box.pack(fill='both', expand=True, pady=(8, 0))
        self.log = tk.Text(box, height=12, wrap='none', state='disabled', relief='solid', borderwidth=1)
        sb = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.pump)
        if start_folder:
            root.after(300, self.start)

    # ── 버튼 ──
    def pick(self):
        d = filedialog.askdirectory(title='지면 이미지 폴더 선택', initialdir=self.folder.get() or None)
        if d:
            self.folder.set(os.path.normpath(d))

    def edit_keywords(self):
        p = os.path.join(sf.HERE, '키워드.txt')
        if not os.path.isfile(p):
            messagebox.showerror(APP_NAME, f'키워드.txt가 없습니다.\n{p}')
            return
        if os.name == 'nt':
            os.system(f'start "" notepad "{p}"')

    def open_result(self):
        if self.result and os.path.isfile(self.result):
            open_path(self.result)

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        folder = self.folder.get().strip().strip('"')
        if not folder:
            self.pick()
            folder = self.folder.get().strip()
            if not folder:
                return
        self.settings['last_folder'] = folder
        save_settings(self.settings)
        self.stop_flag = False
        self.result = None
        self.btn_run.config(state='disabled')
        self.btn_stop.config(state='normal')
        self.btn_open.config(state='disabled')
        self.bar['value'] = 0
        self.clear_log()
        self.status.set('읽는 중…')
        self.worker = threading.Thread(target=self.work, args=(folder,), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag = True
        self.status.set('중단하는 중… (지금 읽고 있는 면까지만 마칩니다)')

    def close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, '아직 읽는 중입니다. 그래도 닫을까요?'):
                return
            self.stop_flag = True
        self.root.destroy()

    # ── 작업 스레드 ──
    def work(self, folder):
        try:
            out = sf.run(folder,
                         log=lambda m: self.q.put(('log', m)),
                         progress=lambda d, t: self.q.put(('prog', (d, t))),
                         stop=lambda: self.stop_flag)
            self.q.put(('done', out))
        except sf.SetupError as e:
            self.q.put(('fail', str(e)))
        except Exception as e:  # 예상 못 한 오류도 창에 보여 줌
            import traceback
            self.q.put(('log', traceback.format_exc()))
            self.q.put(('fail', f'오류가 났습니다: {e}'))

    def pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == 'log':
                    self.add_log(val)
                elif kind == 'prog':
                    d, t = val
                    self.bar['maximum'] = max(t, 1)
                    self.bar['value'] = d
                    self.status.set(f'{d} / {t}면 읽음')
                elif kind == 'done':
                    self.finish()
                    self.result = val
                    self.btn_open.config(state='normal')
                    self.status.set('완료 — 결과 화면을 엽니다.')
                    open_path(val)
                elif kind == 'fail':
                    self.finish()
                    self.status.set(val.splitlines()[0])
                    messagebox.showwarning(APP_NAME, val)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def finish(self):
        self.btn_run.config(state='normal')
        self.btn_stop.config(state='disabled')

    def add_log(self, m):
        self.log.configure(state='normal')
        self.log.insert('end', m + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')


def main():
    start = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None  # 폴더를 exe 위에 끌어다 놓은 경우
    root = tk.Tk()
    App(root, start)
    root.mainloop()


if __name__ == '__main__':
    main()
