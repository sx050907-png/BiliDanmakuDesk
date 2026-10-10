# coding: utf-8
# Copyright (c) 2026 霜星. All rights reserved.
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core import (Cancelled, Client, calibrate, export_all, mapped_time, parse_time,
                  read_xml, timestamp, validate_options, visible_rows)
from preview import MoviePreview
from appearance import display_dialog

BASE = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
RESOURCE = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
STATE = Path(os.environ.get('LOCALAPPDATA', str(BASE))) / 'BiliDanmakuDesk'
ENGINE = RESOURCE / 'vendor' / 'DanmakuFactory.exe'
DEFAULT = {'offset': 0., 'rate': 1., 'size': 36, 'opacity': 80,
           'travel': 8., 'area': 0.72, 'nonoverlap': True, 'segments': [],
           'font':'Microsoft YaHei', 'block_words':[]}


def find_player():
    candidates = [Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'DAUM/PotPlayer/PotPlayerMini64.exe',
                  Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'DAUM/PotPlayer/PotPlayerMini.exe']
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for name in ('PotPlayerMini64.exe', 'PotPlayerMini.exe'):
                try:
                    with winreg.OpenKey(hive, 'SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\' + name) as key:
                        candidates.insert(0, Path(winreg.QueryValueEx(key, '')[0]))
                except OSError:
                    pass
    except ImportError:
        pass
    return next((str(p) for p in candidates if p.is_file()), '')


def probe_duration(movie):
    candidates = [shutil.which('ffprobe')]
    minecraft = Path(os.environ.get('APPDATA', '')) / '.minecraft/ffmpeg/bin/ffprobe.exe'
    if minecraft.is_file():
        candidates.append(str(minecraft))
    executable = next((p for p in candidates if p), None)
    if not executable:
        return None
    process = subprocess.run([executable, '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'json', movie], capture_output=True, timeout=20,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if process.returncode:
        return None
    return float(json.loads(process.stdout)['format']['duration'])


class App:
    def __init__(self, root, state_dir=STATE):
        self.root = root
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.rows = []
        self.meta = None
        self.catalog = None
        self.last_export = None
        self.last_project = None
        self.local_duration = None
        root.title('弹幕同行 · BiliDanmakuDesk 0.13')
        self.app_icon=tk.PhotoImage(file=str(RESOURCE/'assets'/'icon.png'))
        root.iconphoto(True,self.app_icon)
        width, height = 1080, min(720, root.winfo_screenheight() - 100)
        root.geometry(f'{width}x{height}+{max(20, (root.winfo_screenwidth()-width)//2)}+30')
        root.minsize(900, 600)
        root.configure(bg='#f3f5fa')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#f3f5fa', foreground='#26334d')
        style.configure('TButton', padding=(12, 7), background='#e7ecf6', borderwidth=0)
        style.map('TButton', background=[('active', '#d5def0')])
        style.configure('Accent.TButton', background='#3468e8', foreground='white')
        style.map('Accent.TButton', background=[('active', '#2454c9'), ('disabled', '#a3b8e6')])
        style.configure('TLabelframe', padding=8, bordercolor='#dce3ee')
        style.configure('TLabelframe.Label', font=('Microsoft YaHei UI', 11, 'bold'))
        style.configure('TEntry', fieldbackground='white', padding=5)
        style.configure('Treeview', rowheight=25, fieldbackground='white', background='white')
        container = ttk.Frame(root, padding=(24, 12))
        container.pack(fill='both', expand=True)
        ttk.Label(container, text='弹幕同行', font=('Microsoft YaHei UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(container, text='B站弹幕 × 你的本地片源  ·  为 PotPlayer 导出同步弹幕  ·  作者：霜星', foreground='#62718c').pack(anchor='w', pady=(0, 12))
        source = ttk.LabelFrame(container, text='1  找到弹幕和视频')
        source.pack(fill='x', pady=(0, 10))
        source.columnconfigure(1, weight=1)
        self.url = tk.StringVar()
        self.movie = tk.StringVar()
        self.output = tk.StringVar(value=str(BASE / '导出'))
        self.player = tk.StringVar(value=find_player())
        self.ffmpeg = tk.StringVar()
        self.movie_preview = None
        self.refresh = tk.BooleanVar(value=False)
        ttk.Label(source, text='B站链接').grid(row=0, column=0, padx=(0, 12), sticky='w')
        self.url_entry = ttk.Entry(source, textvariable=self.url)
        self.url_entry.grid(row=0, column=1, sticky='ew', padx=(0, 8))
        self.parse_button = ttk.Button(source, text='解析链接', command=self.parse)
        self.parse_button.grid(row=0, column=2)
        self.combo = ttk.Combobox(source, state='readonly')
        self.combo.grid(row=1, column=1, sticky='ew', padx=(0, 8), pady=6)
        self.combo.bind('<<ComboboxSelected>>', self.select_episode)
        ttk.Label(source, text='剧集 / 分P').grid(row=1, column=0, sticky='w')
        self.download_button = ttk.Button(source, text='下载弹幕', command=self.download, style='Accent.TButton')
        self.download_button.grid(row=1, column=2)
        ttk.Label(source, text='本地视频').grid(row=2, column=0, sticky='w')
        ttk.Entry(source, textvariable=self.movie).grid(row=2, column=1, sticky='ew', padx=(0, 8))
        ttk.Button(source, text='选择视频…', command=self.choose_movie).grid(row=2, column=2)
        self.info = tk.StringVar(value='支持 ep / ss / BV / av 链接和编号、b23.tv 短链接。')
        ttk.Label(source, textvariable=self.info, foreground='#62718c').grid(row=3, column=0, columnspan=3, sticky='w', pady=(8, 0))
        bar = ttk.Frame(source)
        bar.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(5, 0))
        self.import_button = ttk.Button(bar, text='导入 XML / 项目…', command=self.import_file)
        self.import_button.pack(side='left')
        ttk.Checkbutton(bar, text='重新获取最新弹幕（不使用分段缓存）', variable=self.refresh).pack(side='left', padx=12)

        review=ttk.LabelFrame(container,text='2  看电影，核对弹幕')
        review.pack(fill='x',pady=(0,14))
        ttk.Label(review,text='播放电影，直接调整弹幕时间、删减片段、字体和屏蔽词。',foreground='#62718c').pack(anchor='w',pady=(4,10))
        self.movie_preview_button=ttk.Button(review,text='打开电影与弹幕核对',command=self.open_movie_preview,style='Accent.TButton')
        self.movie_preview_button.pack(side='left',pady=(0,8))
        self.advanced_window=tk.Toplevel(root)
        self.advanced_window.title('高级设置与弹幕明细')
        self.advanced_window.geometry('1060x530')
        self.advanced_window.transient(root)
        self.advanced_window.protocol('WM_DELETE_WINDOW',self.advanced_window.withdraw)
        self.advanced_window.withdraw()
        advanced=ttk.Frame(self.advanced_window,padding=16)
        advanced.pack(fill='both',expand=True)
        ttk.Button(review,text='高级设置与弹幕明细…',command=self.show_advanced).pack(side='right',pady=(0,8))
        sync = ttk.LabelFrame(advanced, text='精确时间与显示设置')
        sync.pack(fill='x', pady=(0, 10))
        self.options = {key: tk.StringVar(value=str(DEFAULT[key])) for key in ('offset', 'rate', 'size', 'opacity', 'travel')}
        self.area = tk.StringVar(value='72')
        self.nonoverlap = tk.BooleanVar(value=True)
        self.font_name = 'Microsoft YaHei'
        self.block_words = []
        labels = [('offset', '整体偏移（秒）'), ('rate', '时间倍率'), ('size', '字号'), ('opacity', '显示强度 %'), ('travel', '滚动时间（秒）')]
        for col, (key, label) in enumerate(labels):
            ttk.Label(sync, text=label).grid(row=0, column=col * 2, sticky='w', padx=(0, 5))
            ttk.Entry(sync, textvariable=self.options[key], width=8).grid(row=0, column=col * 2 + 1, padx=(0, 12))
        ttk.Label(sync, text='偏移为正 → 弹幕晚出现；偏移为负 → 提前。倍率默认 1，不按总时长自动拉伸。', foreground='#62718c').grid(row=1, column=0, columnspan=10, sticky='w', pady=6)
        line = ttk.Frame(sync)
        line.grid(row=2, column=0, columnspan=10, sticky='w')
        ttk.Label(line, text='显示区域 %').pack(side='left')
        ttk.Entry(line, textvariable=self.area, width=6).pack(side='left', padx=(6, 16))
        ttk.Checkbutton(line, text='避免同类弹幕重叠（拥挤时隐藏部分）', variable=self.nonoverlap).pack(side='left')
        ttk.Button(line, text='双点校准…', command=self.calibration_dialog).pack(side='left', padx=12)
        ttk.Button(line, text='恢复默认', command=self.reset_options).pack(side='left')
        ttk.Label(sync, text='分段偏移：每行“B站起始时间, 额外偏移秒”；从该时间起替换额外偏移，例如 00:10:00, -15。', foreground='#62718c').grid(row=3, column=0, columnspan=10, sticky='w', pady=(7, 3))
        self.segment_text = tk.Text(sync, height=2, font=('Consolas', 10), relief='flat', highlightthickness=1, highlightbackground='#dce3ee')
        self.segment_text.grid(row=4, column=0, columnspan=10, sticky='ew')
        ttk.Button(sync, text='弹幕字体、速度与屏蔽词…', command=self.display_settings).grid(row=5,column=0,columnspan=10,sticky='w',pady=(6,0))

        preview = ttk.LabelFrame(advanced, text='弹幕时间明细')
        preview.pack(fill='both', expand=True, pady=(0, 10))
        self.tree = ttk.Treeview(preview, columns=('original', 'mapped', 'text'), show='headings', height=3)
        for column, title, width in [('original', 'B站原始时间', 130), ('mapped', '本地出现时间', 130), ('text', '弹幕内容', 660)]:
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=column == 'text')
        scrollbar = ttk.Scrollbar(preview, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        footer = ttk.LabelFrame(container, text='3  导出与播放')
        footer.pack(fill='x')
        footer.columnconfigure(1, weight=1)
        ttk.Label(footer, text='导出到').grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(footer, textvariable=self.output).grid(row=0, column=1, sticky='ew')
        ttk.Button(footer, text='选择目录…', command=self.choose_output).grid(row=0, column=2, padx=8)
        actions = ttk.Frame(footer)
        actions.grid(row=1, column=0, columnspan=3, sticky='ew', pady=7)
        self.export_button = ttk.Button(actions, text='导出 ASS + 保存项目', command=self.export, style='Accent.TButton')
        self.export_button.pack(side='left')
        self.preview_button = ttk.Button(advanced, text='更新弹幕明细', command=self.update_preview)
        self.preview_button.pack(anchor='w',pady=4)
        self.play_button = ttk.Button(actions, text='用 PotPlayer 播放', command=self.play)
        self.play_button.pack(side='left')
        ttk.Button(actions, text='打开导出目录', command=self.open_output).pack(side='left', padx=8)
        self.cancel_button = ttk.Button(actions, text='取消任务', command=self.cancel.set)
        self.cancel_button.pack(side='right')
        self.progress = ttk.Progressbar(footer)
        self.progress.grid(row=2, column=0, columnspan=3, sticky='ew')
        self.status = tk.StringVar(value='粘贴链接并下载弹幕，选择视频后进入电影核对。')
        ttk.Label(footer, textvariable=self.status, foreground='#62718c', wraplength=980).grid(row=3, column=0, columnspan=3, sticky='w', pady=(6, 0))
        self.task_controls = [self.parse_button, self.download_button, self.import_button, self.export_button, self.play_button, self.movie_preview_button]
        self.load_settings()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)

    def show_advanced(self):
        self.update_preview()
        self.advanced_window.deiconify()
        self.advanced_window.lift()

    def log(self, text):
        self.status.set(text)

    def set_busy(self, value):
        self.busy = value
        for control in self.task_controls:
            control.configure(state='disabled' if value else 'normal')
        self.combo.configure(state='disabled' if value else 'readonly')

    def worker(self, operation, done):
        if self.busy:
            return
        self.cancel.clear()
        self.set_busy(True)
        def run():
            try:
                result = operation()
                self.messages.put(('done', done, result))
            except Exception as error:
                self.messages.put(('error', error))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:
                event = self.messages.get_nowait()
                if event[0] == 'done':
                    self.set_busy(False)
                    try:
                        event[1](event[2])
                    except Exception as error:
                        self.log(str(error))
                        messagebox.showerror('操作失败', str(error), parent=self.root)
                elif event[0] == 'error':
                    self.set_busy(False)
                    self.log(str(event[1]))
                    if not isinstance(event[1], Cancelled):
                        messagebox.showerror('操作失败', str(event[1]), parent=self.root)
                elif event[0] == 'progress':
                    i, total, count = event[1:]
                    self.progress.configure(value=100 * i / total)
                    self.log(f'下载分段 {i}/{total}，已收集 {count:,} 条弹幕…')
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def parse(self):
        text = self.url.get().strip()
        if not text:
            messagebox.showinfo('输入链接', '先粘贴 B站链接。', parent=self.root)
            return
        self.log('正在解析链接…')
        self.worker(lambda: Client(self.cancel.is_set).metadata(text), self.parsed)

    def parsed(self, catalog):
        if not catalog['items']:
            raise ValueError('这个页面没有可读取的剧集。')
        self.catalog = catalog
        self.combo.configure(values=[i['title'] for i in catalog['items']])
        self.combo.current(catalog['selected'])
        self.select_episode()
        self.log(f"解析成功：{catalog['title']}，共 {len(catalog['items'])} 集 / 分P。")

    def select_episode(self, _event=None):
        if not self.catalog or self.combo.current() < 0:
            return
        self.meta = dict(self.catalog['items'][self.combo.current()])
        self.meta['series'] = self.catalog['title']
        self.rows = []
        self.last_export = self.last_project = None
        self.tree.delete(*self.tree.get_children())
        self.show_info()

    def show_info(self):
        if not self.meta:
            return
        text = f"{self.meta.get('series', '')}  {self.meta['title']}   ·   B站 {timestamp(self.meta.get('duration', 0))}"
        if self.local_duration is not None:
            text += f'   /   本地 {timestamp(self.local_duration)}（须按对应画面核对同步）'
        self.info.set(text)

    def download(self):
        if not self.meta:
            messagebox.showinfo('解析链接', '请先解析链接并选择剧集。', parent=self.root)
            return
        item = dict(self.meta)
        refresh = self.refresh.get()
        self.progress.configure(value=0)
        self.log('正在获取弹幕分段信息…')
        self.worker(lambda: Client(self.cancel.is_set).download(item, self.state_dir / 'cache',
                    lambda i, total, count: self.messages.put(('progress', i, total, count)), refresh), self.downloaded)

    def downloaded(self, rows):
        self.rows = rows
        self.last_export = self.last_project = None
        self.update_preview()
        self.log(f'已获取 {len(rows):,} 条当前弹幕。不是全部历史记录；可以调整后导出。')

    def choose_movie(self):
        value = filedialog.askopenfilename(title='选择你的本地视频', filetypes=[('视频', '*.mp4 *.mkv *.avi *.mov *.webm *.ts *.m4v'), ('所有文件', '*.*')])
        if value:
            self.movie.set(value)
            self.last_export = None
            self.local_duration = None
            if not self.busy:
                self.worker(lambda: probe_duration(value), self.probed)

    def probed(self, value):
        self.local_duration = value
        self.show_info()
        self.log('已选择本地视频。' + ('时长读取完成，时长差不会自动用作偏移。' if value else '未找到 ffprobe；仍可手动校准和导出。'))

    def choose_output(self):
        value = filedialog.askdirectory(title='选择导出目录')
        if value:
            self.output.set(value)

    def get_options(self):
        result = {key: float(var.get()) for key, var in self.options.items()}
        result['size'] = int(result['size'])
        result['opacity'] = int(result['opacity'])
        result['area'] = float(self.area.get()) / 100
        result['nonoverlap'] = self.nonoverlap.get()
        result['font'] = self.font_name
        result['block_words'] = list(self.block_words)
        result['segments'] = []
        for line in self.segment_text.get('1.0', 'end').strip().splitlines():
            if not line.strip():
                continue
            pieces = line.replace('，', ',').split(',')
            if len(pieces) != 2:
                raise ValueError('分段偏移每行应为：原始起始时间, 额外偏移秒')
            result['segments'].append({'start': parse_time(pieces[0]), 'offset': float(pieces[1])})
        result['segments'].sort(key=lambda r: r['start'])
        validate_options(result)
        return result

    def apply_options(self, values):
        values = {**DEFAULT, **values}
        for key, var in self.options.items():
            var.set(str(values[key]))
        self.area.set(str(round(values['area'] * 100, 2)))
        self.nonoverlap.set(values['nonoverlap'])
        self.font_name = values['font']
        self.block_words = list(values['block_words'])
        self.segment_text.delete('1.0', 'end')
        self.segment_text.insert('1.0', '\n'.join(f"{timestamp(r['start'])}, {r['offset']}" for r in values['segments']))

    def reset_options(self):
        self.apply_options(DEFAULT)
        self.update_preview()

    def display_settings(self):
        try:
            options = self.get_options()
        except ValueError as error:
            messagebox.showerror('检查设置',str(error),parent=self.root)
            return
        def apply(updated):
            self.apply_options(updated)
            self.update_preview()
            self.save_settings()
        display_dialog(self.root,options,apply)

    def update_preview(self):
        try:
            options = self.get_options()
        except ValueError as error:
            messagebox.showerror('检查设置', str(error), parent=self.root)
            return
        self.tree.delete(*self.tree.get_children())
        # Display an evenly spaced sample, retaining exact timestamps.
        filtered = visible_rows(self.rows, options)
        step = max(1, len(filtered) // 500)
        for row in filtered[::step]:
            original = row['progress_ms'] / 1000
            local = mapped_time(original, options)
            self.tree.insert('', 'end', values=(timestamp(original), timestamp(local) if local >= 0 else '视频开始前（不显示）', row['content']))

    def import_file(self, path=None):
        path = path or filedialog.askopenfilename(title='导入原始 XML 或导出的项目 JSON', filetypes=[('弹幕 / 项目', '*.xml *.json')])
        if not path:
            return
        try:
            if Path(path).suffix.lower() == '.json':
                record = json.loads(Path(path).read_text(encoding='utf-8-sig'))
                if record.get('version') != 1 or not isinstance(record.get('rows'), list):
                    raise ValueError('请选择本工具导出的项目.json。')
                self.rows, self.meta = record['rows'], record['metadata']
                self.apply_options(record['options'])
                self.movie.set(self.meta.get('movie', ''))
                self.url.set(self.meta.get('url', ''))
                self.last_project = Path(path)
            else:
                self.rows, cid = read_xml(path)
                self.meta = {'cid': cid, 'title': Path(path).stem, 'duration': max((r['progress_ms']/1000 for r in self.rows), default=0)}
                self.last_project = None
            self.catalog = None
            self.combo.configure(values=[self.meta['title']])
            self.combo.current(0)
            self.last_export = None
            self.show_info()
            self.update_preview()
            self.log(f'已导入 {len(self.rows):,} 条弹幕，可以离线调整并导出。')
        except Exception as error:
            messagebox.showerror('导入失败', str(error), parent=self.root)

    def calibration_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('用两个对应画面校准')
        dialog.transient(self.root)
        dialog.grab_set()
        panel = ttk.Frame(dialog, padding=20)
        panel.pack()
        ttk.Label(panel, text='找两个相同画面，分别记录 B站和本地的时间。\n适合逐渐变大的时间差；中途删减请使用分段偏移。').grid(row=0, column=0, columnspan=3, pady=(0, 14))
        variables = [tk.StringVar(value=v) for v in ('00:01:00', '00:01:00', '00:20:00', '00:20:00')]
        ttk.Label(panel, text='B站时间').grid(row=1, column=1)
        ttk.Label(panel, text='本地时间').grid(row=1, column=2)
        for row in range(2):
            ttk.Label(panel, text=f'对应画面 {row+1}').grid(row=row+2, column=0, padx=10, pady=6)
            for column in range(2):
                ttk.Entry(panel, textvariable=variables[row*2+column], width=16).grid(row=row+2, column=column+1, padx=5)
        def apply():
            try:
                rate, offset = calibrate(*(v.get() for v in variables))
                self.options['rate'].set(f'{rate:.9f}')
                self.options['offset'].set(f'{offset:.6f}')
                self.update_preview()
                dialog.destroy()
            except ValueError as error:
                messagebox.showerror('检查对应时间', str(error), parent=dialog)
        ttk.Button(panel, text='应用校准', command=apply, style='Accent.TButton').grid(row=4, column=0, columnspan=3, pady=12)

    def export(self, after=None):
        if not self.meta or not self.rows:
            messagebox.showinfo('没有弹幕', '先下载弹幕，或导入 XML / 项目。', parent=self.root)
            return
        try:
            options = self.get_options()
            metadata = {**self.meta, 'movie': self.movie.get().strip()}
            output = self.output.get().strip()
            if not output:
                raise ValueError('请选择导出目录。')
            if metadata['movie'] and not Path(metadata['movie']).is_file():
                raise ValueError('本地视频路径不存在，请重新选择。')
        except ValueError as error:
            messagebox.showerror('检查设置', str(error), parent=self.root)
            return
        self.save_settings()
        name = f"{metadata.get('series', '')} {metadata['title']}".strip()
        rows = list(self.rows)
        self.log('正在生成 ASS 并保存项目…')
        def complete(result):
            destination, stats = result
            self.last_export = next(destination.glob('*.ass'))
            self.last_project = destination / '项目.json'
            self.log(f"已导出 {stats['rendered']:,} 条；{stats['skipped']:,} 条因时间、密度或类型未显示。保存到 {destination}")
            if after:
                after()
        self.worker(lambda: export_all(output, name, rows, metadata, options, ENGINE), complete)

    def play(self, position=None):
        if not self.movie.get() or not Path(self.movie.get()).is_file():
            messagebox.showinfo('选择视频', '先选择本地视频。', parent=self.root)
            return
        player = self.player.get()
        if not Path(player).is_file():
            player = filedialog.askopenfilename(title='选择 PotPlayerMini64.exe 或 PotPlayerMini.exe', filetypes=[('播放器', '*.exe')])
            if not player:
                return
            self.player.set(player)
        def launch():
            # Always regenerate so changing settings cannot play a stale ASS.
            args = [player, movie, '/sub=' + str(self.last_export), '/new']
            if position is not None:
                args.append('/seek=' + timestamp(position))
            subprocess.Popen(args, shell=False)
            self.log('已调用 PotPlayer。若没有显示弹幕，请检查字幕是否开启、ASS 样式是否被覆盖。')
        movie = self.movie.get()
        self.export(after=launch)

    def open_movie_preview(self, start=None):
        if not self.rows or not self.meta:
            messagebox.showinfo('没有弹幕', '请先下载或导入弹幕。', parent=self.root)
            return
        movie = self.movie.get().strip()
        if not movie or not Path(movie).is_file():
            messagebox.showinfo('选择视频', '请先选择对应的本地电影。', parent=self.root)
            return
        try:
            options = self.get_options()
        except ValueError as error:
            messagebox.showerror('检查设置', str(error), parent=self.root)
            return
        library = RESOURCE / 'vendor' / 'libmpv-2.dll'
        self.save_settings()
        if self.movie_preview and not self.movie_preview.closed:
            self.movie_preview.window.lift()
            return
        supplied_start = start is not None
        start = 0. if start is None else float(start)
        selected = self.tree.selection()
        if selected and not supplied_start:
            value = self.tree.item(selected[0], 'values')[1]
            try:
                start = parse_time(value)
            except ValueError:
                pass
        def apply(values):
            self.apply_options(values)
            self.update_preview()
            self.save_settings(strict=True)
            self.log('调整已保存，可以导出。')
        def show(duration):
            self.local_duration = duration
            self.movie_preview = MoviePreview(self.root, movie, self.rows,
                {**self.meta, 'source_input':self.url.get().strip()}, options,
                library, ENGINE, duration, start, apply, lambda pos:self.play(position=pos))
        try:
            show(self.local_duration)
        except Exception as error:
            messagebox.showerror("无法打开核对播放器", str(error), parent=self.root)

    def open_output(self):
        folder = self.last_export.parent if self.last_export else Path(self.output.get())
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def load_settings(self):
        try:
            data = json.loads((self.state_dir / 'settings.json').read_text(encoding='utf-8'))
            self.output.set(data.get('output', self.output.get()))
            self.player.set(data.get('player', self.player.get()))
            self.ffmpeg.set(data.get('ffmpeg', ''))
            self.apply_options(data.get('options', DEFAULT))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def save_settings(self, strict=False):
        try:
            data = {'output': self.output.get(), 'player': self.player.get(), 'ffmpeg': self.ffmpeg.get(), 'options': self.get_options()}
            target = self.state_dir / 'settings.json'
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)
        except (OSError, ValueError):
            if strict:
                raise

    def close(self):
        if self.busy:
            self.cancel.set()
            self.log('正在取消任务，请等待任务结束后关闭。')
            return
        self.save_settings()
        if self.movie_preview and not self.movie_preview.closed:
            self.movie_preview.close()
        self.root.destroy()


if __name__ == '__main__':
    if os.name == 'nt':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except OSError:
            pass
    root = tk.Tk()
    application = App(root)
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file() and Path(sys.argv[1]).suffix.lower() in ('.xml', '.json'):
        def open_initial():
            application.import_file(sys.argv[1])
            if '--preview' in sys.argv:
                when = next((arg.split('=',1)[1] for arg in sys.argv if arg.startswith('--seek=')), '0')
                try:
                    application.open_movie_preview(start=parse_time(when))
                except ValueError as error:
                    messagebox.showerror('检查预览时间', str(error), parent=root)
        root.after(150, open_initial)
    root.mainloop()
