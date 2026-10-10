# coding: utf-8
# Copyright (c) 2026 霜星. All rights reserved.
"""Native movie playback with a simple, live danmaku timing adjustment."""
import json
import queue
import os
import shutil
import tempfile
import threading
import time
import ctypes
from ctypes import wintypes
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from tkinter import filedialog, messagebox, ttk
from core import factory_ass, mapped_time, parse_time, timestamp, validate_options, visible_rows
from appearance import display_dialog
from player import Player


def adjusted_options(options, delta):
    result = json.loads(json.dumps(options))
    result['offset'] += delta
    validate_options(result)
    return result


def align_from(options, bili_time, movie_time):
    """Align this original-time anchor and everything after it, preserving later edits."""
    if bili_time < 0 or movie_time < 0:
        raise ValueError('两个时间都不能为负数。')
    result = json.loads(json.dumps(options))
    delta = movie_time-mapped_time(bili_time, options)
    active = 0.
    for rule in options['segments']:
        if rule['start'] <= bili_time:
            active = rule['offset']
    result['segments'] = [r for r in result['segments'] if r['start'] != bili_time]
    for rule in result['segments']:
        if rule['start'] > bili_time:
            rule['offset'] += delta
    result['segments'].append({'start':bili_time, 'offset':active+delta})
    result['segments'].sort(key=lambda r:r['start'])
    validate_options(result)
    return result


def original_time_at(movie_time, options):
    """Find a source boundary on a mapped timeline, including inserted gaps."""
    rules = [{'start':0., 'offset':0.}]+options['segments']
    candidates = []
    for index, rule in enumerate(rules):
        end = rules[index+1]['start'] if index+1 < len(rules) else float('inf')
        source = (movie_time-options['offset']-rule['offset'])/options['rate']
        if rule['start'] <= source < end:
            candidates.append(source)
        elif source < rule['start'] < end:
            candidates.append(rule['start'])
    return max(0., min(candidates))


def insert_local_scene(options, start, end):
    """Leave room for footage present locally but cut from the source version."""
    if start < 0 or end <= start:
        raise ValueError('请先标记开始，再在后面的画面标记结束。')
    anchor = round(original_time_at(start, options), 2)
    return align_from(options, anchor, mapped_time(anchor, options) + end-start)


def inserted_scene_ranges(options):
    """Movie intervals reserved by positive jumps in the piecewise mapping."""
    ranges=[]
    previous=0.
    for rule in options['segments']:
        if rule['offset']>previous:
            begin=rule['start']*options['rate']+options['offset']+previous
            ranges.append((begin,begin+rule['offset']-previous))
        previous=rule['offset']
    return ranges


def resize_local_scene(options,index,start,end):
    ranges=inserted_scene_ranges(options)
    if start<0 or end<=start or index<0 or index>=len(ranges):
        raise ValueError('片段结束时间必须在开始之后。')
    if any(start<b and end>a for i,(a,b) in enumerate(ranges) if i!=index):
        raise ValueError('删减片段不能互相重叠。')
    previous=0.;positive=[]
    for rule in options['segments']:
        if rule['offset']>previous:positive.append(rule)
        previous=rule['offset']
    rule=positive[index]
    length=ranges[index][1]-ranges[index][0]
    base=align_from(options,rule['start'],mapped_time(rule['start'],options)-length)
    previous=0.;clean=[]
    for item in base['segments']:
        if abs(item['offset']-previous)>.000001:clean.append(item)
        previous=item['offset']
    base['segments']=clean
    return insert_local_scene(base,start,end)


def timeline_bins(rows, options, duration, count, start=0., span=None):
    """Index downloaded comments on the mapped movie timeline, once per change."""
    bins = [[] for _ in range(count)]
    span = duration if span is None else span
    if duration <= 0 or span <= 0:
        return bins
    for row in visible_rows(rows, options):
        seconds = mapped_time(row['progress_ms']/1000, options)
        if 0 <= seconds < duration and start <= seconds < start+span:
            bins[min(count-1, int((seconds-start)/span*count))].append(row)
    return bins


def timeline_drag_delta(pixels, width, duration, fine=False):
    return pixels/max(1, width-28)*duration/(10 if fine else 1)


def rounded_box(canvas,left,top,right,bottom,radius,**style):
    return canvas.create_polygon(left+radius,top,right-radius,top,right,top,right,top+radius,
                                 right,bottom-radius,right,bottom,right-radius,bottom,left+radius,bottom,
                                 left,bottom,left,bottom-radius,left,top+radius,left,top,
                                 smooth=True,splinesteps=24,**style)


class MoviePreview:
    def __init__(self, parent, movie, rows, metadata, options, library, engine,
                 duration, start, on_apply, on_potplayer):
        self.closed = False
        self.player = None
        self.options = json.loads(json.dumps(options))
        self.initial_options = json.loads(json.dumps(options))
        self.base_offset = self.options['offset']
        self.undo_stack, self.redo_stack = [], []
        self.ass_serial = 0
        self.cut_anchor = None
        self.adjust_scope = tk.StringVar(value='all')
        self.fullscreen_overlay = None
        self.fullscreen_scrubbing = False
        self.fullscreen_mouse_down = False
        self.danmaku_visible = tk.BooleanVar(value=True)
        self.playback_rate = tk.StringVar(value='1×')
        self.right_pressed = False
        self.right_fast = False
        self.right_timer = None
        self.movie_mouse_down = False
        self.movie_click_origin = None
        self.movie_click_dragged = False
        self.cut_dialog = None
        self.scene_selection = None
        self.marker_drag = None
        self.sync_cancel = None
        self.sync_dialog = None
        self.movie = movie
        self.timeline_zoom, self.timeline_start = 1., 0.
        self.rows, self.metadata, self.engine = rows, metadata, engine
        self.on_apply, self.on_potplayer = on_apply, on_potplayer
        self.duration = duration or 0.
        self.position, self.playing, self.scrubbing = float(start), False, False
        self.ready, self.loaded, self.load_ticks = False, False, 0
        self.events = queue.Queue()
        self.temporary = tempfile.TemporaryDirectory(prefix='bili_native_')
        self.ass_path = Path(self.temporary.name) / 'danmaku.ass'
        self.window = w = tk.Toplevel(parent)
        w.title('电影与弹幕核对 · 拖动进度条，边看边调整')
        w.geometry('1080x850')
        w.minsize(760, 650)
        w.protocol('WM_DELETE_WINDOW', self.close)
        self.panel = panel = ttk.Frame(w, padding=12)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='看电影，核对弹幕', font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(panel, text='点击画面或空格播放／暂停 · ←／→ 后退／前进 5 秒 · 长按 → 临时 3 倍速。').pack(anchor='w', pady=(4, 10))
        self.video = tk.Frame(panel, background='#101827', height=180)
        self.video.pack(fill='both', expand=True)
        self.video.pack_propagate(False)
        distribution_header = ttk.Frame(panel)
        distribution_header.pack(fill='x', pady=(8, 0))
        self.distribution_summary = tk.StringVar(value='全片弹幕分布 · 正在读取电影时长…')
        ttk.Label(distribution_header, textvariable=self.distribution_summary,
                  foreground='#6553a8').pack(side='left')
        self.drag_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(distribution_header, text='允许整体拖动', variable=self.drag_enabled,
                        command=self.toggle_drag_lock).pack(side='left', padx=12)
        ttk.Button(distribution_header, text='全片', width=5, command=self.reset_zoom).pack(side='right')
        ttk.Button(distribution_header, text='＋', width=3,
                   command=lambda:self.zoom_timeline(1.5, self.position)).pack(side='right')
        ttk.Button(distribution_header, text='－', width=3,
                   command=lambda:self.zoom_timeline(1/1.5, self.position)).pack(side='right')
        self.zoom_label = tk.StringVar(value='1× · Alt＋滚轮缩放')
        ttk.Label(distribution_header, textvariable=self.zoom_label,
                  foreground='#58657c').pack(side='right', padx=8)
        self.distribution = tk.Canvas(panel, height=58, background='#edf2fa',
                                      highlightthickness=0, cursor='arrow')
        self.distribution.pack(fill='x', pady=(3, 0))
        self.distribution_key = None
        self.distribution_bins = []
        self.dragging_danmaku = False
        self.distribution.bind('<Configure>', lambda e:self.draw_distribution())
        self.distribution.bind('<ButtonPress-1>', self.danmaku_drag_begin)
        self.distribution.bind('<B1-Motion>', self.danmaku_drag_move)
        self.distribution.bind('<ButtonRelease-1>', self.danmaku_drag_end)
        self.distribution.bind('<Motion>', self.hover_distribution)
        self.distribution.bind('<Leave>', lambda e:self.clear_distribution_hover())
        self.distribution.bind('<MouseWheel>', self.timeline_wheel)
        self.distribution.bind('<Alt-MouseWheel>', self.timeline_wheel)
        self.timeline_scroll = ttk.Scrollbar(panel, orient='horizontal', command=self.pan_timeline)
        self.timeline_scroll.pack(fill='x')
        self.distribution_hint = tk.StringVar(value='弹幕条已锁定 · 勾选“允许整体拖动”后可调整；Alt＋滚轮缩放仍可使用。')
        ttk.Label(panel, textvariable=self.distribution_hint, foreground='#6553a8',
                  anchor='w').pack(fill='x')
        self.progress = tk.Canvas(panel, height=68, background='#edf2fa', highlightthickness=0, cursor='hand2')
        self.progress.pack(fill='x', pady=(2, 0))
        self.progress.bind('<Configure>', lambda e:self.draw_progress())
        self.progress.bind('<ButtonPress-1>', self.scrub_begin)
        self.progress.bind('<B1-Motion>', self.scrub_move)
        self.progress.bind('<ButtonRelease-1>', self.scrub_end)
        self.progress.bind('<MouseWheel>',self.timeline_wheel)
        self.progress.bind('<Motion>',self.marker_hover)
        self.progress.bind('<Escape>',self.cancel_marker_drag)
        timing = ttk.Frame(panel)
        timing.pack(fill='x')
        self.time_label = tk.StringVar(value='正在打开电影…')
        ttk.Label(timing, textvariable=self.time_label).pack(side='left')
        ttk.Label(timing, text='橙色片段可拖动 · Alt＋滚轮放大', foreground='#426ca8').pack(side='right')
        ttk.Checkbutton(timing,text='显示弹幕',variable=self.danmaku_visible,command=self.toggle_danmaku).pack(side='right',padx=12)
        rate_box=ttk.Combobox(timing,textvariable=self.playback_rate,values=('0.5×','1×','1.25×','1.5×','2×','3×','4×'),state='readonly',width=6)
        rate_box.pack(side='right')
        rate_box.bind('<<ComboboxSelected>>',lambda e:self.set_playback_rate())
        ttk.Label(timing,text='播放速度').pack(side='right',padx=6)
        controls = ttk.Frame(panel)
        controls.pack(fill='x', pady=8)
        self.play_button = ttk.Button(controls, text='▶ 播放', command=self.toggle_play, width=12)
        self.play_button.pack(side='left')
        ttk.Button(controls, text='后退 10 秒', command=lambda:self.seek(self.position-10)).pack(side='left', padx=5)
        ttk.Button(controls, text='前进 10 秒', command=lambda:self.seek(self.position+10)).pack(side='left')
        ttk.Label(controls, text='音量').pack(side='left', padx=(24, 4))
        self.volume = tk.DoubleVar(value=65)
        ttk.Scale(controls, from_=0, to=100, variable=self.volume, length=110,
                  command=self.set_volume).pack(side='left')
        self.muted = False
        self.mute_button = ttk.Button(controls, text='静音', command=self.toggle_mute, width=7)
        self.mute_button.pack(side='left', padx=4)
        self.fullscreen = False
        self.fullscreen_button = ttk.Button(controls, text='画面全屏 F11', command=self.toggle_fullscreen)
        self.fullscreen_button.pack(side='right')
        ttk.Button(controls,text='弹幕显示设置…',command=self.display_settings).pack(side='right',padx=6)
        adjustment = ttk.LabelFrame(panel, text='弹幕和电影对不上？', padding=10)
        adjustment.pack(fill='x', pady=4)
        row = ttk.Frame(adjustment)
        row.pack(fill='x')
        self.early = ttk.Button(row, text='弹幕早了 → 推迟', command=lambda:self.adjust_timing(self.step.get()), style='Accent.TButton')
        self.early.pack(side='left')
        self.late = ttk.Button(row, text='弹幕晚了 → 提前', command=lambda:self.adjust_timing(-self.step.get()), style='Accent.TButton')
        self.late.pack(side='left', padx=8)
        ttk.Label(row, text='每次').pack(side='left', padx=(10, 3))
        self.step = tk.DoubleVar(value=.5)
        box = ttk.Combobox(row, textvariable=self.step, values=(.1, .5, 1, 5), state='readonly', width=4)
        box.pack(side='left')
        ttk.Label(row, text='秒').pack(side='left', padx=4)
        ttk.Button(row, text='恢复本次打开时的设置', command=self.reset).pack(side='right')
        self.redo_button = ttk.Button(row, text='重做', width=6, command=self.redo, state='disabled')
        self.redo_button.pack(side='right', padx=4)
        self.undo_button = ttk.Button(row, text='撤销', width=6, command=self.undo, state='disabled')
        self.undo_button.pack(side='right')
        scope = ttk.Frame(adjustment)
        scope.pack(fill='x',pady=(6,0))
        ttk.Label(scope,text='调整范围：').pack(side='left')
        ttk.Radiobutton(scope,text='整部电影',variable=self.adjust_scope,value='all',command=self.update_scope_label).pack(side='left')
        ttk.Button(scope,text='从当前画面开始，前面不动',command=self.start_cut_adjustment).pack(side='left',padx=8)
        self.scope_label = tk.StringVar(value='正在调整整部电影')
        ttk.Label(scope,textvariable=self.scope_label,foreground='#215caf').pack(side='left')
        self.offset_label = tk.StringVar()
        ttk.Label(adjustment, textvariable=self.offset_label, foreground='#215caf').pack(anchor='w', pady=(8, 0))
        self.update_offset_label()
        bottom = ttk.Frame(panel)
        bottom.pack(fill='x', pady=(8, 0))
        self.advanced_button = ttk.Button(bottom, text='更多核对方式 ▾', command=self.toggle_advanced)
        self.advanced_button.pack(side='left')
        ttk.Button(bottom, text='从 B 站链接自动校准…', command=self.auto_sync).pack(side='left', padx=6)
        ttk.Button(bottom, text='选择 B 站删减片段…', command=self.segment_dialog).pack(side='left')
        ttk.Button(bottom, text='保存调整', command=self.save_and_return, style='Accent.TButton').pack(side='right')
        ttk.Button(bottom, text='保存并用 PotPlayer 播放', command=self.open_pot).pack(side='right', padx=8)
        self.advanced = ttk.Frame(panel, padding=(0, 8))
        jump = ttk.Frame(self.advanced)
        jump.pack(fill='x')
        ttk.Label(jump, text='跳到时间').pack(side='left')
        self.time_entry = tk.StringVar(value=timestamp(start))
        ttk.Entry(jump, textvariable=self.time_entry, width=14).pack(side='left', padx=6)
        ttk.Button(jump, text='跳转', command=self.seek_entry).pack(side='left')
        ttk.Label(jump, text='  附近的弹幕（双击跳到出现位置）').pack(side='left')
        self.tree = ttk.Treeview(self.advanced, columns=('time', 'text'), show='headings', height=4)
        self.tree.heading('time', text='电影时间');self.tree.heading('text', text='弹幕内容')
        self.tree.column('time', width=105, stretch=False);self.tree.column('text', width=650)
        self.tree.pack(fill='x', pady=5)
        self.tree.bind('<Double-1>', self.jump_selected)
        alignment = ttk.Frame(self.advanced)
        alignment.pack(fill='x')
        ttk.Button(alignment, text='整片对齐选中弹幕', command=self.align_selected).pack(side='left')
        ttk.Button(alignment, text='只从选中弹幕开始对齐…', command=self.advanced_segment_dialog).pack(side='left', padx=8)
        self.status = tk.StringVar(value='正在准备弹幕；视频按原帧率播放，支持声音。')
        self.status_widget = ttk.Label(panel, textvariable=self.status, foreground='#58657c')
        self.status_widget.pack(anchor='w', pady=(8, 0))
        w.bind('<space>', self.space_key)
        w.bind('<Control-z>', self.undo)
        w.bind('<Control-y>', self.redo)
        w.bind('<F11>',self.toggle_fullscreen)
        w.bind('<Escape>',self.exit_fullscreen)
        w.bind('<KeyPress-Right>',self.right_down)
        w.bind('<KeyRelease-Right>',self.right_up)
        w.bind('<KeyPress-Left>',self.left_key)
        w.bind('<FocusOut>',lambda e:w.after_idle(self.check_hold_focus))
        w.update_idletasks()
        try:
            self.player = Player(library, self.video.winfo_id())
            self.player.set('start', max(0., start))
            self.player.command('loadfile', str(movie))
        except Exception:
            self.close()
            raise
        def prepare():
            try:
                ass, _ = factory_ass(rows, metadata, options, engine)
                self.events.put(('ass', (ass, json.loads(json.dumps(options)))))
            except Exception as error:
                self.events.put(('error', str(error)))
        threading.Thread(target=prepare, daemon=False).start()
        w.after(80, self.poll)

    def space_key(self, event):
        if isinstance(event.widget, (ttk.Entry, ttk.Combobox)):
            return
        if self.fullscreen:
            self.fullscreen_activity=time.monotonic()
            self.show_fullscreen_controls()
        self.toggle_play()
        return 'break'

    def toggle_danmaku(self):
        if self.player:
            self.player.set('sub-visibility','yes' if self.danmaku_visible.get() else 'no')

    def set_playback_rate(self):
        rate=float(self.playback_rate.get().rstrip('×'))
        if self.player:
            self.player.set('speed',3 if self.right_fast else rate)

    def right_down(self,event):
        if isinstance(event.widget,(ttk.Entry,ttk.Combobox,tk.Entry,tk.Text)):
            return
        if not self.right_pressed:
            self.right_pressed=True
            self.right_timer=self.window.after(350,self.begin_fast_forward)
        return 'break'

    def left_key(self,event):
        if isinstance(event.widget,(ttk.Entry,ttk.Combobox,tk.Entry,tk.Text)):
            return
        self.seek(self.position-5)
        return 'break'

    @staticmethod
    def native_mouse_sample():
        user=ctypes.windll.user32
        user.GetForegroundWindow.restype=wintypes.HWND
        user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
        pid=wintypes.DWORD()
        user.GetWindowThreadProcessId(user.GetForegroundWindow(),ctypes.byref(pid))
        if pid.value!=os.getpid():
            return False,False
        state=user.GetAsyncKeyState(1)
        return bool(state&0x8000),bool(state&1)

    def pointer_over_movie(self,pointer):
        x,y=pointer
        video=self.video
        if not (video.winfo_rootx()<=x<video.winfo_rootx()+video.winfo_width() and
                video.winfo_rooty()<=y<video.winfo_rooty()+video.winfo_height()):
            return False
        # Floating controls and dialogs must not be interpreted as a movie click.
        user=ctypes.windll.user32
        user.WindowFromPoint.argtypes=[wintypes.POINT]
        user.WindowFromPoint.restype=wintypes.HWND
        user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
        user.GetAncestor.restype=wintypes.HWND
        target=user.WindowFromPoint(wintypes.POINT(x,y))
        if user.GetAncestor(target,2)!=user.GetAncestor(video.winfo_id(),2):
            return False
        if self.fullscreen_overlay and self.fullscreen_overlay.winfo_viewable():
            overlay=self.fullscreen_overlay
            if overlay.winfo_rootx()<=x<overlay.winfo_rootx()+overlay.winfo_width() and overlay.winfo_rooty()<=y<overlay.winfo_rooty()+overlay.winfo_height():
                return False
        return True

    def poll_movie_click(self,pointer,down,pressed):
        if (down and not self.movie_mouse_down) or (pressed and not self.movie_mouse_down):
            self.movie_click_origin=pointer if self.pointer_over_movie(pointer) else None
            self.movie_click_dragged=False
        if self.movie_click_origin:
            if max(abs(pointer[i]-self.movie_click_origin[i]) for i in (0,1))>8:
                self.movie_click_dragged=True
            if not down:
                if not self.movie_click_dragged and self.pointer_over_movie(pointer):
                    self.toggle_play()
                    self.window.focus_set()
                self.movie_click_origin=None
        self.movie_mouse_down=down

    def begin_fast_forward(self):
        self.right_timer=None
        if self.right_pressed and not self.closed:
            self.right_fast=True
            self.player.set('speed',3)
            if self.fullscreen:
                self.fullscreen_activity=time.monotonic()
                self.show_fullscreen_controls()

    def right_up(self,event=None):
        if not self.right_pressed:
            return
        fast=self.right_fast
        self.cancel_fast_forward()
        if not fast:
            self.seek(self.position+5)
        return 'break'

    def cancel_fast_forward(self):
        if self.right_timer:
            self.window.after_cancel(self.right_timer)
            self.right_timer=None
        self.right_pressed=False
        self.right_fast=False
        if self.player and not self.closed:
            self.set_playback_rate()

    def check_hold_focus(self):
        if self.closed or not self.right_pressed:
            return
        focused=self.window.focus_displayof()
        if not focused or focused.winfo_toplevel() not in (self.window,self.fullscreen_overlay,getattr(self,'fullscreen_menu',None)):
            self.cancel_fast_forward()

    def fullscreen_rate_menu(self,event):
        old=getattr(self,'fullscreen_menu',None)
        if old and old.winfo_exists():
            self.close_fullscreen_menu()
            return
        menu=tk.Toplevel(self.fullscreen_overlay)
        self.fullscreen_menu=menu
        menu.overrideredirect(True)
        menu.attributes('-topmost',True)
        menu.attributes('-transparentcolor','#010203')
        menu.attributes('-alpha',.96)
        menu.configure(background='#010203')
        values=('0.5×','1×','1.25×','1.5×','2×','3×','4×')
        width,height=144,272
        x=min(self.window.winfo_rootx()+self.window.winfo_width()-width-20,max(self.window.winfo_rootx()+20,event.x_root-72))
        y=max(self.window.winfo_rooty()+20,self.fullscreen_overlay.winfo_rooty()-height-4)
        menu.geometry(f'{width}x{height}+{x}+{y}')
        canvas=tk.Canvas(menu,background='#010203',highlightthickness=0,cursor='hand2')
        canvas.pack(fill='both',expand=True)
        def draw(hover=-1):
            canvas.delete('all')
            rounded_box(canvas,0,0,width,height,14,fill='#242a35',outline='')
            canvas.create_text(18,23,text='播放速度',anchor='w',fill='#b6bfce',font=('Microsoft YaHei UI',10))
            for index,value in enumerate(values):
                top=42+index*31
                if index==hover:
                    rounded_box(canvas,8,top,width-8,top+29,8,fill='#3a4352',outline='')
                selected=value==self.playback_rate.get()
                canvas.create_text(24,top+15,text=value,anchor='w',fill='#fb7299' if selected else 'white',font=('Microsoft YaHei UI',12))
                if selected:canvas.create_text(width-26,top+15,text='✓',fill='#fb7299',font=('Microsoft YaHei UI',12))
        def close():
            self.close_fullscreen_menu()
        def choose(value):
            self.playback_rate.set(value)
            self.set_playback_rate()
            close()
            self.window.focus_set()
        def click(e):
            index=int((e.y-42)//31)
            if e.y>=42 and 0<=index<len(values):choose(values[index])
        canvas.bind('<Motion>',lambda e:draw(int((e.y-42)//31) if e.y>=42 else -1))
        canvas.bind('<Leave>',lambda e:draw())
        canvas.bind('<Button-1>',click)
        menu.bind('<Escape>',lambda e:close())
        menu.bind('<space>',self.space_key)
        def lost_focus():
            if menu.winfo_exists():
                focus=menu.focus_displayof()
                if focus and focus.winfo_toplevel()==menu:return
                pointer=self.window.winfo_pointerxy()
                c=self.fullscreen_progress
                if self.rate_hit(pointer[0]-c.winfo_rootx(),pointer[1]-c.winfo_rooty(),c.winfo_width()):return
                close()
        menu.bind('<FocusOut>',lambda e:menu.after_idle(lost_focus))
        draw()
        menu.focus_set()

    def close_fullscreen_menu(self):
        menu=getattr(self,'fullscreen_menu',None)
        if menu and menu.winfo_exists():menu.destroy()
        self.fullscreen_menu=None
        if not self.closed:self.window.focus_set()

    @staticmethod
    def rate_hit(x,y,width):
        return width-252<=x<=width-158 and 52<=y<=82

    def toggle_fullscreen(self, event=None):
        self.cancel_fast_forward()
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.fullscreen_layout = [(child, child.pack_info()) for child in self.panel.winfo_children() if child.winfo_manager() == 'pack']
            for child, _ in self.fullscreen_layout:
                child.pack_forget()
            self.panel.configure(padding=0)
            self.video.pack(fill='both',expand=True)
        else:
            if self.fullscreen_overlay:
                self.fullscreen_overlay.destroy()
                self.fullscreen_overlay = None
            if self.fullscreen_scrubbing:
                self.scrubbing = False
            self.fullscreen_scrubbing = False
            self.fullscreen_mouse_down = False
            self.video.pack_forget()
            self.panel.configure(padding=12)
            for child, layout in self.fullscreen_layout:
                child.pack(**layout)
        self.window.attributes('-fullscreen', self.fullscreen)
        if self.fullscreen:
            self.window.update_idletasks()
            self.create_fullscreen_controls()
        self.window.focus_force()
        self.fullscreen_button.configure(text='退出全屏 Esc' if self.fullscreen else '画面全屏 F11')
        return 'break'

    def create_fullscreen_controls(self):
        overlay = self.fullscreen_overlay = tk.Toplevel(self.window)
        overlay.overrideredirect(True)
        overlay.attributes('-topmost',True)
        overlay.configure(background='#010203')
        overlay.attributes('-transparentcolor','#010203')
        self.fullscreen_last_pointer = self.window.winfo_pointerxy()
        self.fullscreen_activity = time.monotonic()
        self.fullscreen_progress = tk.Canvas(overlay,height=96,background='#010203',highlightthickness=0,cursor='hand2')
        self.fullscreen_progress.pack(fill='both',expand=True)
        self.fullscreen_progress.bind('<ButtonPress-1>',self.fullscreen_seek_begin)
        self.fullscreen_progress.bind('<B1-Motion>',self.fullscreen_seek_move)
        self.fullscreen_progress.bind('<ButtonRelease-1>',self.fullscreen_seek_end)
        for sequence, callback in [('<space>',self.space_key),('<F11>',self.toggle_fullscreen),('<Escape>',self.exit_fullscreen),
                                   ('<KeyPress-Right>',self.right_down),('<KeyRelease-Right>',self.right_up),('<KeyPress-Left>',self.left_key)]:
            overlay.bind(sequence,callback)
        overlay.bind('<FocusOut>',lambda e:self.window.after_idle(self.check_hold_focus))
        self.show_fullscreen_controls()

    def show_fullscreen_controls(self):
        overlay=self.fullscreen_overlay
        if not overlay:
            return
        width,height=self.window.winfo_width(),self.window.winfo_height()
        overlay.geometry(f'{width}x96+{self.window.winfo_rootx()}+{self.window.winfo_rooty()+height-96}')
        overlay.deiconify()
        overlay.lift()

    def poll_fullscreen_controls(self):
        if not self.fullscreen or not self.fullscreen_overlay:
            return
        pointer=self.window.winfo_pointerxy()
        now=time.monotonic()
        menu=getattr(self,'fullscreen_menu',None)
        menu_open=menu and menu.winfo_exists() and menu.winfo_viewable()
        if pointer != self.fullscreen_last_pointer or self.fullscreen_scrubbing or self.marker_drag or self.right_fast or menu_open:
            self.fullscreen_last_pointer=pointer
            self.fullscreen_activity=now
            self.show_fullscreen_controls()
        elif now-self.fullscreen_activity>=3:
            focused=self.window.focus_get()
            owned_focus=focused and focused.winfo_toplevel()==self.fullscreen_overlay
            self.fullscreen_overlay.withdraw()
            if owned_focus:
                self.window.focus_force()
        # Color-keyed pixels are visually transparent and pass native clicks through.
        # Handle the complete control hit areas while this app owns the foreground.
        down=self.mouse_button_down()
        c=self.fullscreen_progress
        event=SimpleNamespace(x=pointer[0]-c.winfo_rootx(),y=pointer[1]-c.winfo_rooty(),
                              x_root=pointer[0],y_root=pointer[1])
        if down and not self.fullscreen_mouse_down:
            self.fullscreen_mouse_down=True
            if menu_open and not (menu.winfo_rootx()<=pointer[0]<menu.winfo_rootx()+menu.winfo_width() and menu.winfo_rooty()<=pointer[1]<menu.winfo_rooty()+menu.winfo_height()):
                if not self.rate_hit(event.x,event.y,c.winfo_width()):self.close_fullscreen_menu()
            if self.fullscreen_overlay.winfo_viewable() and 0<=event.x<c.winfo_width() and 14<=event.y<=88:
                self.fullscreen_seek_begin(event)
        elif down and (self.fullscreen_scrubbing or self.marker_drag):
            self.fullscreen_seek_move(event)
        elif not down and self.fullscreen_mouse_down:
            self.fullscreen_seek_end(event)
            self.fullscreen_mouse_down=False
        if not self.fullscreen_overlay:
            return
        if self.fullscreen_overlay.winfo_viewable():
            self.draw_fullscreen_controls(pointer)

    @staticmethod
    def mouse_button_down():
        user=ctypes.windll.user32
        user.GetForegroundWindow.restype=ctypes.c_void_p
        user.GetWindowThreadProcessId.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint)]
        pid=ctypes.c_uint()
        user.GetWindowThreadProcessId(user.GetForegroundWindow(),ctypes.byref(pid))
        return pid.value==os.getpid() and bool(user.GetAsyncKeyState(1)&0x8000)

    def draw_fullscreen_controls(self,pointer):
        c=self.fullscreen_progress
        width=max(80,c.winfo_width())
        mx,my=pointer[0]-c.winfo_rootx(),pointer[1]-c.winfo_rooty()
        on_track=14<=my<=44 and 28<=mx<=width-28
        x=28+(width-56)*min(1,max(0,self.position/max(.001,self.duration)))
        pink='#fb7299'
        c.delete('all')
        c.create_line(28,30,width-28,30,fill='#34404c',width=5,capstyle='round')
        c.create_line(28,30,width-28,30,fill='#a3adb8',width=4 if on_track else 2,capstyle='round')
        c.create_line(28,30,x,30,fill=pink,width=4 if on_track else 3,capstyle='round')
        self.draw_scene_markers(c,28,width-28,30,fullscreen=True)
        radius=6 if on_track or self.fullscreen_scrubbing else 3
        c.create_oval(x-radius,30-radius,x+radius,30+radius,fill=pink,outline='')
        def text(x,y,label,anchor='w',color='white',size=12):
            c.create_text(x+1,y+1,text=label,anchor=anchor,font=('Microsoft YaHei UI',size),fill='#161b22')
            c.create_text(x,y,text=label,anchor=anchor,font=('Microsoft YaHei UI',size),fill=color)
        def color(left,right):
            return pink if 46<=my<=88 and left<=mx<=right else 'white'
        play_color=color(20,56)
        if self.playing:
            for left in (32,43):c.create_line(left,58,left,76,fill=play_color,width=5,capstyle='round')
        else:
            c.create_polygon(31,57,30,59,30,75,31,77,33,77,47,69,48,67,47,65,33,57,
                             smooth=True,splinesteps=24,fill=play_color,outline='')
        volume_color=color(65,104)
        c.create_polygon(73,63,79,63,87,57,87,77,79,71,73,71,fill=volume_color,outline='')
        if self.muted:
            c.create_line(92,62,100,72,fill=volume_color,width=2)
            c.create_line(92,72,100,62,fill=volume_color,width=2)
        else:
            c.create_arc(80,56,101,78,start=-60,extent=120,style='arc',outline=volume_color,width=2)
        text(120,67,f'{timestamp(self.position).split(".")[0]} / {timestamp(self.duration).split(".")[0]}',size=11)
        dm_color=color(width-425,width-300)
        left=width-415
        rounded_box(c,left,58,left+18,76,4,fill='',outline=dm_color,width=2)
        if self.danmaku_visible.get():
            c.create_line(left+3,66,left+8,71,left+15,62,fill=pink,width=2,capstyle='round',joinstyle='round')
        text(left+28,67,'弹幕',color=dm_color)
        rate='3×' if self.right_fast else self.playback_rate.get()
        rate_color=pink if self.rate_hit(mx,my,width) else 'white'
        text(width-218,67,'倍速' if rate=='1×' else rate,anchor='center',color=rate_color)
        c.create_polygon(width-178,64,width-168,64,width-173,70,fill=rate_color,outline='')
        exit_color=color(width-95,width-20)
        for x1,y1,dx,dy in [(width-66,57,7,7),(width-46,57,-7,7),(width-66,77,7,-7),(width-46,77,-7,-7)]:
            c.create_line(x1+dx,y1,x1,y1,x1,y1+dy,fill=exit_color,width=2,capstyle='round',joinstyle='round')
        if on_track:
            hovered=min(1,max(0,(mx-28)/max(1,width-56)))*self.duration
            text(min(width-60,max(60,mx)),8,timestamp(hovered).split('.')[0],anchor='center',size=10)
        elif 46<=my<=88:
            label=('播放／暂停（空格）' if 20<=mx<=56 else '静音／取消静音' if 65<=mx<=104 else
                   '显示／隐藏弹幕' if width-425<=mx<=width-300 else '播放倍速' if self.rate_hit(mx,my,width) else
                   '退出全屏（Esc）' if width-95<=mx<=width-20 else '')
            if label:text(min(width-90,max(90,mx)),8,label,anchor='center',size=10)

    def fullscreen_seek_begin(self,event):
        self.fullscreen_mouse_down=True
        self.fullscreen_activity=time.monotonic()
        if self.begin_marker_drag(event,self.fullscreen_progress,28,self.fullscreen_progress.winfo_width()-28,fullscreen=True):
            return
        if event.y<22 and self.seek_scene_marker(self.fullscreen_progress,event):
            return
        if event.y > 44:
            width=self.fullscreen_progress.winfo_width()
            if 20<=event.x<=56:
                self.toggle_play()
            elif 65<=event.x<=104:
                self.toggle_mute()
            elif width-95<=event.x<=width-20:
                self.exit_fullscreen()
            elif width-425<=event.x<=width-300:
                self.danmaku_visible.set(not self.danmaku_visible.get())
                self.toggle_danmaku()
            elif self.rate_hit(event.x,event.y,width):
                self.fullscreen_rate_menu(event)
            return
        if self.loaded:
            self.fullscreen_scrubbing=True
            self.scrubbing=True
            self.fullscreen_progress.focus_set()
            self.fullscreen_seek_move(event)

    def fullscreen_seek_move(self,event):
        if self.marker_drag:
            self.move_marker_drag(event);return
        if self.fullscreen_scrubbing:
            fraction=(event.x-28)/max(1,self.fullscreen_progress.winfo_width()-56)
            self.position=min(1,max(0,fraction))*self.duration
            self.draw_progress()
            self.fullscreen_activity=time.monotonic()

    def fullscreen_seek_end(self,event):
        self.fullscreen_mouse_down=False
        if self.marker_drag:
            self.end_marker_drag(event);return
        if self.fullscreen_scrubbing:
            self.fullscreen_seek_move(event)
            self.fullscreen_scrubbing=False
            self.scrubbing=False
            self.seek(self.position)
            self.fullscreen_activity=time.monotonic()

    def exit_fullscreen(self, event=None):
        self.cancel_marker_drag()
        if self.fullscreen:
            self.toggle_fullscreen()
        return 'break'

    def display_settings(self):
        if not self.ready or self.dragging_danmaku:
            return
        def apply(updated):
            if not self.ready:
                return False
            self.replace_options(updated)
        display_dialog(self.window, self.options, apply)

    def set_volume(self, value):
        if self.player:
            self.player.set('volume', value)

    def toggle_mute(self):
        self.muted = not self.muted
        self.player.set('mute', 'yes' if self.muted else 'no')
        self.mute_button.configure(text='取消静音' if self.muted else '静音')

    def draw_progress(self):
        c = self.progress
        width = max(40, c.winfo_width())
        left, right, y = 14, width-14, 52
        span=self.duration/self.timeline_zoom
        x = left+(right-left)*min(1, max(0, (self.position-self.timeline_start)/max(.001,span)))
        c.delete('all')
        c.create_line(left, y, right, y, fill='#ccd8e9', width=8, capstyle='round')
        c.create_line(left, y, x, y, fill='#2676ee', width=8, capstyle='round')
        self.draw_scene_markers(c,left,right,y)
        c.create_oval(x-8, y-8, x+8, y+8, fill='#2676ee', outline='white', width=2)
        self.time_label.set(f'{timestamp(self.position)}  /  {timestamp(self.duration)}')
        self.draw_distribution()

    def scene_markers(self):
        ranges=[(a,b,True) for a,b in inserted_scene_ranges(self.options)]
        if self.marker_drag:
            i=self.marker_drag['index']
            ranges[i]=(*self.marker_drag['range'],False)
        selection=self.scene_selection
        if selection and selection['start'] is not None:
            a,b=selection['start'],selection['end']
            if b is None:
                ranges.append((a,a,False))
            elif not any(abs(a-x)<.03 and abs(b-y)<.03 for x,y,_ in ranges):
                ranges.append((a,b,False))
        return ranges

    def draw_scene_markers(self,canvas,left,right,y,fullscreen=False):
        ranges=self.scene_markers()
        origin=0. if fullscreen else self.timeline_start
        span=self.duration if fullscreen else self.duration/self.timeline_zoom
        marker_y=y if fullscreen else 28
        for i,(start,end,applied) in enumerate(ranges):
            if end<origin or start>origin+span:continue
            a=left+(right-left)*max(0,min(1,(start-origin)/max(.001,span)))
            b=left+(right-left)*max(0,min(1,(end-origin)/max(.001,span)))
            color='#ffb449' if applied else '#ee8b23'
            tag=f'scene-marker-{i}'
            if end>start:
                canvas.create_line(a,marker_y,b,marker_y,fill=color,width=8,capstyle='round',tags=tag)
            for point in (a,b) if end>start else (a,):
                canvas.create_line(point,marker_y-8,point,marker_y+8,fill=color,width=3,tags=tag)
                canvas.create_polygon(point-4,marker_y-12,point+4,marker_y-12,point,marker_y-7,fill=color,outline='',tags=tag)
            if i==len(ranges)-1:
                interval=timestamp(start).split('.')[0]
                if end>start:interval+='–'+timestamp(end).split('.')[0]
                label=f'B 站删减片段 {interval} · '+('已调整' if applied else '待调整' if end>start else '请选择结束')
                center=max(left+190,min(right-190,(a+b)/2))
                if fullscreen:
                    canvas.create_text(center+1,9,text=label,fill='#161b22',font=('Microsoft YaHei UI',10),tags=tag)
                canvas.create_text(center,8 if fullscreen else 11,text=label,fill=color if fullscreen else '#925115',font=('Microsoft YaHei UI',10),tags=tag)

    def marker_target(self,event,left,right,fullscreen=False):
        if not (18<=event.y<=40 if fullscreen else 16<=event.y<=38):return None
        origin=0. if fullscreen else self.timeline_start
        span=self.duration if fullscreen else self.duration/self.timeline_zoom
        for i,(start,end) in enumerate(inserted_scene_ranges(self.options)):
            if end<origin or start>origin+span:continue
            a=left+(right-left)*(start-origin)/max(.001,span)
            b=left+(right-left)*(end-origin)/max(.001,span)
            near_a=abs(event.x-a)<=7 and left<=a<=right
            near_b=abs(event.x-b)<=7 and left<=b<=right
            if near_a or near_b:
                return i,('start' if near_a and (not near_b or abs(event.x-a)<=abs(event.x-b)) else 'end')
            if a<=event.x<=b:return i,'body'
        return None

    def begin_marker_drag(self,event,canvas,left,right,fullscreen=False):
        if not self.ready or self.dragging_danmaku or self.scene_selection:return False
        target=self.marker_target(event,left,right,fullscreen)
        if target is None:return False
        index,kind=target
        self.marker_drag={'index':index,'kind':kind,'x':event.x,'range':inserted_scene_ranges(self.options)[index],
                          'original':inserted_scene_ranges(self.options)[index],
                          'options':json.loads(json.dumps(self.options)),
                          'scale':(self.duration if fullscreen else self.duration/self.timeline_zoom)/max(1,right-left),
                          'canvas':canvas,'changed':False}
        canvas.focus_set();canvas.grab_set()
        self.status.set('拖动两端改变起止时间；拖动中间移动片段。Alt＋滚轮放大，Shift 精调；Esc 取消。')
        return True

    def move_marker_drag(self,event):
        drag=self.marker_drag
        if not drag:return
        if abs(event.x-drag['x'])<3 and not drag['changed']:return
        delta=(event.x-drag['x'])*drag['scale']*(.1 if getattr(event,'state',0)&1 else 1.)
        a,b=drag['original']
        if drag['kind']=='start':a=max(0,min(b-.05,a+delta))
        elif drag['kind']=='end':b=max(a+.05,min(self.duration,b+delta))
        else:
            delta=max(-a,min(self.duration-b,delta));a+=delta;b+=delta
        drag['range']=(round(a,2),round(b,2));drag['changed']=True
        self.draw_progress()

    def end_marker_drag(self,event):
        self.move_marker_drag(event)
        drag=self.marker_drag
        if not drag:return
        self.marker_drag=None
        drag['canvas'].grab_release()
        if not drag['changed']:
            self.seek(drag['original'][0]);return
        try:
            updated=resize_local_scene(drag['options'],drag['index'],*drag['range'])
            self.replace_options(updated)
        except ValueError as error:
            self.status.set(str(error)+' 已保留原来的标记。')
        self.draw_progress()

    def cancel_marker_drag(self,event=None):
        if self.marker_drag:
            self.marker_drag['canvas'].grab_release();self.marker_drag=None
            self.draw_progress();self.status.set('已取消标记拖动。')
            return 'break'

    def marker_hover(self,event):
        if not self.marker_drag:
            target=self.marker_target(event,14,self.progress.winfo_width()-14)
            self.progress.configure(cursor='sb_h_double_arrow' if target else 'hand2')

    def seek_scene_marker(self,canvas,event):
        for item in canvas.find_overlapping(event.x-2,event.y-2,event.x+2,event.y+2):
            for tag in canvas.gettags(item):
                if tag.startswith('scene-marker-'):
                    index=int(tag.rsplit('-',1)[1])
                    ranges=self.scene_markers()
                    if index<len(ranges):
                        self.seek(ranges[index][0]);return True
        return False

    def draw_distribution(self):
        c = self.distribution
        width = max(40, c.winfo_width())
        left, right, bottom = 14, width-14, 51
        count = max(1, int((right-left)/4))
        span = self.duration/self.timeline_zoom
        key = (width, self.duration, self.timeline_start, span, json.dumps(self.options, sort_keys=True))
        if key != self.distribution_key:
            self.distribution_key = key
            self.distribution_bins = timeline_bins(self.rows, self.options, self.duration, count,
                                                    self.timeline_start, span)
            peak = max((len(group) for group in self.distribution_bins), default=0)
            c.delete('all')
            c.create_line(left, bottom, right, bottom, fill='#d3cdec')
            for index, group in enumerate(self.distribution_bins):
                if not group:
                    continue
                ratio = len(group)/peak
                height = max(2, 39*ratio)
                x = left+(right-left)*index/count
                color = '#7660c5' if ratio >= .5 else '#b4a4e4'
                c.create_rectangle(x, bottom-height, x+max(1, (right-left)/count-1),
                                   bottom, fill=color, outline='', tags='bars')
            self.distribution_cursor = c.create_line(left, 4, left, bottom+3,
                                                     fill='#ee8b2d', width=2)
            self.distribution_marker = c.create_polygon(left-5, 1, left+5, 1, left, 7,
                                                       fill='#ee8b2d', outline='')
            total = sum(len(group) for group in self.distribution_bins)
            title = '全片弹幕分布' if self.timeline_zoom == 1 else f'{timestamp(self.timeline_start)}–{timestamp(self.timeline_start+span)}'
            self.distribution_summary.set(f'{title} · {total:,} 条')
            self.clear_distribution_hover()
            if self.duration > 0 and total == 0:
                c.create_text(width/2, 28, text='当前电影时间范围内没有弹幕', fill='#7d7395')
        x = left+(right-left)*(self.position-self.timeline_start)/max(.001, span)
        c.coords(self.distribution_cursor, x, 4, x, bottom+3)
        c.coords(self.distribution_marker, x-5, 1, x+5, 1, x, 7)
        visible = self.timeline_start <= self.position <= self.timeline_start+span
        c.itemconfigure(self.distribution_cursor, state='normal' if visible else 'hidden')
        c.itemconfigure(self.distribution_marker, state='normal' if visible else 'hidden')
        self.timeline_scroll.set(self.timeline_start/max(.001, self.duration),
                                 (self.timeline_start+span)/max(.001, self.duration))
        self.zoom_label.set(f'{self.timeline_zoom:.3g}× · Alt＋滚轮缩放')

    def zoom_timeline(self, factor, anchor, fraction=None):
        if self.duration <= 0 or self.dragging_danmaku or self.marker_drag:
            return
        old_span = self.duration/self.timeline_zoom
        fraction = ((anchor-self.timeline_start)/old_span if fraction is None else fraction)
        fraction = min(1., max(0., fraction))
        self.timeline_zoom = min(2048., max(1., self.timeline_zoom*factor))
        span = self.duration/self.timeline_zoom
        self.timeline_start = min(self.duration-span, max(0., anchor-fraction*span))
        self.draw_progress()

    def reset_zoom(self):
        if self.dragging_danmaku or self.marker_drag:
            return
        self.timeline_zoom, self.timeline_start = 1., 0.
        self.draw_progress()

    def timeline_wheel(self, event):
        # Windows reports Alt as Mod1 or as its platform-specific high bit.
        if not event.state & (0x0008 | 0x20000):
            return
        fraction = min(1., max(0., (event.x-14)/max(1, event.widget.winfo_width()-28)))
        anchor = self.timeline_start+fraction*self.duration/self.timeline_zoom
        self.zoom_timeline(1.5**(event.delta/120), anchor, fraction)
        return 'break'

    def pan_timeline(self, action, value, units=None):
        if self.dragging_danmaku or self.marker_drag or self.duration <= 0:
            return
        span = self.duration/self.timeline_zoom
        if action == 'moveto':
            start = float(value)*self.duration
        else:
            start = self.timeline_start+int(value)*span*(.9 if units == 'pages' else .1)
        self.timeline_start = min(self.duration-span, max(0., start))
        self.draw_progress()

    def hover_distribution(self, event):
        if self.dragging_danmaku or not self.distribution_bins or self.duration <= 0:
            return
        c = self.distribution
        width = max(40, c.winfo_width())
        count = len(self.distribution_bins)
        fraction = min(1., max(0., (event.x-14)/max(1, width-28)))
        index = min(count-1, int(fraction*count))
        group = self.distribution_bins[index]
        span = self.duration/self.timeline_zoom
        start = self.timeline_start+index/count*span
        end = self.timeline_start+(index+1)/count*span
        snippets = ' / '.join(' '.join(r['content'].split())[:22] for r in group[:3])
        label = f'{timestamp(start)}–{timestamp(end)} · {len(group)} 条'
        self.distribution_hint.set(label + (' · '+snippets if snippets else ' · 此时段没有弹幕'))
        c.delete('hover')
        x1, x2 = 14+(width-28)*index/count, 14+(width-28)*(index+1)/count
        c.create_rectangle(x1, 8, x2, 52, outline='#6553a8', tags='hover')

    def clear_distribution_hover(self):
        self.distribution.delete('hover')
        if not self.dragging_danmaku:
            self.distribution_hint.set('向左拖提前，向右拖推迟；拖过 8 像素才开始调整。Shift 精细拖动。'
                                       if self.drag_enabled.get() else
                                       '弹幕条已锁定 · 勾选“允许整体拖动”后可调整；Alt＋滚轮缩放仍可使用。')

    def toggle_drag_lock(self):
        if self.dragging_danmaku:
            self.dragging_danmaku = False
            self.replace_options(self.danmaku_drag_options, remember=False)
        self.distribution.configure(cursor='sb_h_double_arrow' if self.drag_enabled.get() else 'arrow')
        self.clear_distribution_hover()

    def danmaku_drag_begin(self, event):
        self.distribution.focus_set()
        if not self.drag_enabled.get() or not self.ready or self.duration <= 0:
            return
        self.dragging_danmaku = True
        self.distribution.focus_set()
        self.danmaku_drag_origin = event.x
        self.danmaku_drag_options = json.loads(json.dumps(self.options))
        self.danmaku_drag_offset = self.options['offset']
        self.danmaku_drag_fine = bool(event.state & 0x0001)
        self.danmaku_drag_span = self.duration/self.timeline_zoom
        self.danmaku_drag_started = False
        self.distribution.delete('hover')

    def danmaku_drag_move(self, event):
        if not self.dragging_danmaku:
            return
        pixels = event.x-self.danmaku_drag_origin
        if not self.danmaku_drag_started:
            if abs(pixels) <= 8:
                return
            self.danmaku_drag_started = True
        # A dead zone avoids an abrupt offset jump when the threshold is crossed.
        pixels = (1 if pixels >= 0 else -1)*max(0, abs(pixels)-8)
        delta = timeline_drag_delta(pixels,
                                    self.distribution.winfo_width(), self.danmaku_drag_span,
                                    self.danmaku_drag_fine)
        target = self.danmaku_drag_offset+delta
        self.shift(target-self.options['offset'], remember=False)
        direction = '推迟' if delta >= 0 else '提前'
        self.distribution_hint.set(f'正在整体{direction} {abs(delta):.2f} 秒 · 松开后可继续微调并保存')

    def danmaku_drag_end(self, event):
        if self.dragging_danmaku:
            self.danmaku_drag_move(event)
            self.dragging_danmaku = False
            self.remember_options(self.danmaku_drag_options)
            self.clear_distribution_hover()

    def scrub_begin(self, event):
        if not self.loaded:
            return
        if self.begin_marker_drag(event,self.progress,14,self.progress.winfo_width()-14):return
        if event.y<24 and self.seek_scene_marker(self.progress,event):
            return
        self.scrubbing = True
        self.scrub_move(event)

    def scrub_move(self, event):
        if self.marker_drag:
            self.move_marker_drag(event);return
        if not self.scrubbing:
            return
        fraction = (event.x-14)/max(1, self.progress.winfo_width()-28)
        self.position = self.timeline_start+min(1, max(0, fraction))*self.duration/self.timeline_zoom
        self.draw_progress()

    def scrub_end(self, event):
        if self.marker_drag:
            self.end_marker_drag(event);return
        if self.scrubbing:
            self.scrub_move(event)
            self.scrubbing = False
            self.seek(self.position)

    def seek(self, seconds):
        if not self.loaded:
            return
        self.position = max(0, min(float(seconds), max(0, self.duration-.1)))
        self.player.command('seek', self.position, 'absolute+exact')
        self.draw_progress()
        self.refresh_list()

    def seek_entry(self):
        try:
            self.seek(parse_time(self.time_entry.get()))
        except ValueError as error:
            messagebox.showerror('检查时间', str(error), parent=self.window)

    def toggle_play(self):
        if not self.ready:
            return
        self.playing = not self.playing
        self.player.set('pause', 'no' if self.playing else 'yes')
        self.play_button.configure(text='❚❚ 暂停' if self.playing else '▶ 播放')

    def update_offset_label(self):
        value = self.options['offset']
        desc = '推迟' if value >= 0 else '提前'
        self.offset_label.set(f'整体：弹幕{desc} {abs(value):.2f} 秒  ·  分段校准点 {len(self.options["segments"])} 个  ·  Ctrl＋Z 撤销')

    def remember_options(self, previous):
        if previous != self.options:
            self.undo_stack.append(previous)
            self.redo_stack.clear()
        self.update_history_buttons()

    def update_history_buttons(self):
        self.undo_button.configure(state='normal' if self.ready and self.undo_stack else 'disabled')
        self.redo_button.configure(state='normal' if self.ready and self.redo_stack else 'disabled')

    def undo(self, event=None):
        if self.marker_drag:
            self.cancel_marker_drag();return 'break'
        if event and isinstance(event.widget, (ttk.Entry, ttk.Combobox)):
            return
        if self.ready and not self.dragging_danmaku and self.undo_stack:
            previous = self.undo_stack[-1]
            current = json.loads(json.dumps(self.options))
            self.replace_options(previous, remember=False)
            self.undo_stack.pop()
            self.redo_stack.append(current)
            self.update_history_buttons()
            self.status.set('已撤销上一次调整；确认后点击“保存调整”。')
        return 'break'

    def redo(self, event=None):
        if self.marker_drag:
            self.cancel_marker_drag();return 'break'
        if event and isinstance(event.widget, (ttk.Entry, ttk.Combobox)):
            return
        if self.ready and not self.dragging_danmaku and self.redo_stack:
            following = self.redo_stack[-1]
            current = json.loads(json.dumps(self.options))
            self.replace_options(following, remember=False)
            self.redo_stack.pop()
            self.undo_stack.append(current)
            self.update_history_buttons()
        return 'break'

    def shift(self, delta, remember=True):
        if not self.ready:
            return
        updated = adjusted_options(self.options, float(delta))
        self.replace_options(updated, remember)

    def update_scope_label(self):
        self.scope_label.set(f'只调整 {timestamp(self.cut_movie_position)} 之后的弹幕'
                             if self.adjust_scope.get()=='cut' else '正在调整整部电影')

    def start_cut_adjustment(self):
        if not self.ready or self.dragging_danmaku:
            return
        if self.playing:
            self.toggle_play()
        self.cut_movie_position = self.position
        self.cut_anchor = round(original_time_at(self.position,self.options),2)
        self.adjust_scope.set('cut')
        self.update_scope_label()
        self.status.set('已标记起点。播放核对后，用“弹幕早了／晚了”微调后面的弹幕；前面保持原位。')

    def adjust_timing(self,delta):
        if not self.ready:
            return
        if self.adjust_scope.get()=='cut' and self.cut_anchor is not None:
            target=mapped_time(self.cut_anchor,self.options)+float(delta)
            self.replace_options(align_from(self.options,self.cut_anchor,target))
        else:
            self.shift(delta)

    def replace_options(self, updated, remember=True):
        if not self.ready:
            return
        updated = json.loads(json.dumps(updated))
        validate_options(updated)
        previous = json.loads(json.dumps(self.options))
        rebuild = any(updated[k] != previous[k] for k in updated if k != 'offset')
        if rebuild:
            self.pending_restore = (previous, list(self.undo_stack), list(self.redo_stack))
        if not rebuild:
            self.player.set('sub-delay', updated['offset']-self.base_offset)
        self.options = updated
        if remember:
            self.remember_options(previous)
        self.update_offset_label()
        self.draw_distribution()
        self.refresh_list()
        self.status.set('调整已预览；点击“保存调整”用于导出和 PotPlayer。')
        if rebuild:
            self.ready = False
            self.update_history_buttons()
            self.status.set('正在更新校准后的弹幕轨道…')
            def prepare():
                try:
                    ass, _ = factory_ass(self.rows, self.metadata, updated, self.engine)
                    self.events.put(('ass', (ass, updated)))
                except Exception as error:
                    self.events.put(('error', str(error)))
            threading.Thread(target=prepare, daemon=False).start()

    def reset(self):
        self.adjust_scope.set('all')
        self.update_scope_label()
        self.replace_options(self.initial_options)

    def commit(self):
        if self.ready:
            self.on_apply(json.loads(json.dumps(self.options)))
            self.status.set('已保存调整。可以继续核对，也可以关闭窗口后导出。')
            return True
        return False

    def save_and_return(self):
        if self.marker_drag or self.dragging_danmaku:
            self.status.set('请先松开鼠标完成拖动，再保存调整。')
            return
        if not self.ready:
            self.status.set('正在更新弹幕，请稍后再保存调整。')
            return
        try:
            saved=self.commit()
        except Exception as error:
            messagebox.showerror('保存失败',f'调整未能保存，请重试。\n{error}',parent=self.window)
            return
        if saved:
            main=self.window.master
            self.close()
            main.deiconify()
            main.lift()
            main.focus_set()

    def open_pot(self):
        if self.ready:
            self.commit()
            self.player.set('pause', 'yes')
            self.playing = False
            self.play_button.configure(text='▶ 播放')
            self.on_potplayer(self.position)

    def toggle_advanced(self):
        if self.advanced.winfo_manager():
            self.advanced.pack_forget()
            self.advanced_button.configure(text='更多核对方式 ▾')
        else:
            self.advanced.pack(fill='x', before=self.status_widget)
            self.advanced_button.configure(text='收起更多核对方式 ▴')
            self.refresh_list()

    def refresh_list(self):
        if not self.advanced.winfo_manager():
            return
        self.tree.delete(*self.tree.get_children())
        nearby = sorted(visible_rows(self.rows,self.options), key=lambda r:abs(mapped_time(r['progress_ms']/1000, self.options)-self.position))[:12]
        for row in sorted(nearby, key=lambda r:r['progress_ms']):
            seconds = mapped_time(row['progress_ms']/1000, self.options)
            self.tree.insert('', 'end', iid=str(self.rows.index(row)), values=(timestamp(seconds), row['content']))

    def selected(self):
        selection = self.tree.selection()
        return self.rows[int(selection[0])] if selection else None

    def jump_selected(self, event=None):
        row = self.selected()
        if row:
            self.seek(mapped_time(row['progress_ms']/1000, self.options))

    def align_selected(self):
        row = self.selected()
        if row:
            self.shift(self.position-mapped_time(row['progress_ms']/1000, self.options))

    def segment_dialog(self):
        if not self.ready or self.dragging_danmaku:
            return
        if self.cut_dialog and self.cut_dialog.winfo_exists():
            self.cut_dialog.lift()
            return
        if self.playing:
            self.toggle_play()
        dialog=self.cut_dialog=tk.Toplevel(self.window)
        dialog.title('选择 B 站删减片段的起止时间')
        dialog.transient(self.window)
        panel=ttk.Frame(dialog,padding=18)
        panel.pack(fill='both',expand=True)
        ttk.Label(panel,text='选择 B 站删减片段的起止时间',font=('Microsoft YaHei UI',12,'bold')).pack(anchor='w')
        ttk.Label(panel,text='B 站删掉了电影中的一段，但本地电影保留了这一段，\n因此这段没有对应的弹幕。\n在本地电影里选出这段的起止时间，让后面的弹幕从结束后接上。').pack(anchor='w',pady=(8,12))
        state={'start':None,'end':None,'applied':False,'before':None}
        self.scene_selection=state
        ttk.Label(panel,text='拖动电影进度条到开始处和结束处，分别点击下面的按钮。').pack(anchor='w')
        markers=ttk.Frame(panel);markers.pack(fill='x',pady=10)
        def mark(which):
            if not self.ready:
                return
            if self.playing:
                self.toggle_play()
            if which=='end' and (state['start'] is None or self.position<=state['start']):
                messagebox.showinfo('结束位置要在开始之后','请用电影进度条跳到这段多出来的画面结束处，再标记结束。',parent=dialog)
                return
            state[which]=self.position
            if which=='start':
                state['end']=None
            state['applied']=False
            state['before']=None
            refresh_text()
        start=ttk.Button(markers,text='① 当前画面是删减片段的开始',command=lambda:mark('start'),style='Accent.TButton')
        start.pack(fill='x')
        start_time=ttk.Label(markers,text='尚未标记',foreground='#215caf');start_time.pack(anchor='w',pady=(4,10))
        end=ttk.Button(markers,text='② 当前画面是删减片段的结束',command=lambda:mark('end'),style='Accent.TButton')
        end.pack(fill='x')
        end_time=ttk.Label(markers,text='尚未标记',foreground='#215caf');end_time.pack(anchor='w',pady=(4,8))
        result=ttk.Label(panel,text='',foreground='#215caf');result.pack(anchor='w',pady=(0,10))
        def apply():
            if not self.ready or state['applied'] or state['end'] is None:
                return
            state['before']=json.loads(json.dumps(self.options))
            self.replace_options(insert_local_scene(self.options,state['start'],state['end']))
            state['applied']=True
            self.cut_movie_position=state['start']
            self.cut_anchor=round(original_time_at(state['start'],state['before']),2)
            self.adjust_scope.set('cut');self.update_scope_label()
            self.seek(state['start'])
            refresh_text()
        apply_button=ttk.Button(panel,text='③ 按这段时长后移后面的弹幕',command=apply,style='Accent.TButton');apply_button.pack(fill='x')
        tools=ttk.Frame(panel);tools.pack(fill='x',pady=10)
        play=ttk.Button(tools,text='▶ 播放核对',command=self.toggle_play);play.pack(side='left')
        undo=ttk.Button(tools,text='撤销调整',command=self.undo);undo.pack(side='right')
        def save():
            if self.ready and state['applied']:
                self.commit();dialog.destroy()
        done=ttk.Button(panel,text='核对好了，保存',command=save);done.pack(fill='x')
        ttk.Label(panel,text='只移动弹幕，不修改电影，也不会补出 B 站没有的弹幕。',foreground='#62718c').pack(anchor='w',pady=(10,0))
        dialog.bind('<space>',self.space_key)
        dialog.bind('<Control-z>',self.undo);dialog.bind('<Control-y>',self.redo)
        dialog.bind('<KeyPress-Left>',self.left_key)
        dialog.bind('<KeyPress-Right>',self.right_down);dialog.bind('<KeyRelease-Right>',self.right_up)
        def refresh_text():
            if state['before'] is not None:
                expected=insert_local_scene(state['before'],state['start'],state['end'])
                state['applied']=self.options==expected
            start_time.configure(text=timestamp(state['start']) if state['start'] is not None else '尚未标记')
            end_time.configure(text=timestamp(state['end']) if state['end'] is not None else '尚未标记')
            if state['end'] is None:
                text='先标记开始，再找到这段的结束。'
            else:
                length=state['end']-state['start']
                text=(f'已留出 {length:.2f} 秒，后面的弹幕整体后移。\n播放到这段结束，核对弹幕是否恢复同步。' if state['applied'] else
                      f'B 站删掉的这段长 {length:.2f} 秒。\n点击下面的按钮，把后面的弹幕后移 {length:.2f} 秒。')
            result.configure(text=text if self.ready else '正在更新弹幕…')
            start.configure(state='normal' if self.ready else 'disabled')
            end.configure(state='normal' if self.ready and state['start'] is not None and not state['applied'] else 'disabled')
            apply_button.configure(state='normal' if self.ready and state['end'] is not None and not state['applied'] else 'disabled')
            done.configure(state='normal' if self.ready and state['applied'] else 'disabled')
            play.configure(state='normal' if self.ready else 'disabled',text='❚❚ 暂停核对' if self.playing else '▶ 播放核对')
            undo.configure(state='normal' if self.ready and self.undo_stack else 'disabled')
            self.draw_progress()
        def refresh():
            if not dialog.winfo_exists():return
            refresh_text()
            self.cut_dialog_timer=self.window.after(120,refresh)
        def destroyed(event):
            if event.widget==dialog:
                timer=getattr(self,'cut_dialog_timer',None)
                if timer:self.window.after_cancel(timer)
                self.cut_dialog=None
                self.scene_selection=None
                if not self.closed:self.draw_progress()
        dialog.bind('<Destroy>',destroyed)
        refresh()

    def advanced_segment_dialog(self):
        if not self.ready or self.dragging_danmaku:
            return
        row = self.selected()
        if self.playing:
            self.toggle_play()
        dialog = tk.Toplevel(self.window)
        dialog.title('删减／分段校准 · 之前的弹幕保持原位')
        dialog.transient(self.window)
        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='找到两版中同一画面，或一句能对应画面的弹幕。',
                  font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(panel, text='输入它的 B 站时间和本地电影时间，只调整这个点及之后的弹幕。\n'
                  '下一处剪辑再次对不上时，再加一个校准点；无需录制参考素材。').pack(anchor='w', pady=8)
        fields = ttk.Frame(panel)
        fields.pack(fill='x')
        bili = tk.StringVar(value=timestamp(row['progress_ms']/1000) if row else '')
        local = tk.StringVar(value=timestamp(self.position))
        ttk.Label(fields, text='B 站对应时间').grid(row=0, column=0, sticky='w', pady=4)
        entry = ttk.Entry(fields, textvariable=bili, width=18)
        entry.grid(row=0, column=1, padx=8)
        ttk.Label(fields, text='本地对应时间').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Entry(fields, textvariable=local, width=18).grid(row=1, column=1, padx=8)
        ttk.Button(fields, text='使用当前播放位置', command=lambda:local.set(timestamp(self.position))).grid(row=1, column=2)
        ttk.Label(panel, text='也可选择附近一条弹幕，再把电影停在它应该出现的画面：').pack(anchor='w', pady=(12, 4))
        candidates = ttk.Treeview(panel, columns=('bili', 'movie', 'text'), show='headings', height=5)
        for key, label, width in [('bili','B 站原始时间',110),('movie','当前电影时间',110),('text','弹幕内容',340)]:
            candidates.heading(key, text=label)
            candidates.column(key, width=width, stretch=key=='text')
        nearby = sorted(visible_rows(self.rows,self.options), key=lambda r:abs(mapped_time(r['progress_ms']/1000,self.options)-self.position))[:30]
        for i, item in enumerate(sorted(nearby, key=lambda r:r['progress_ms'])):
            candidates.insert('', 'end', iid=str(i), values=(timestamp(item['progress_ms']/1000),
                              timestamp(mapped_time(item['progress_ms']/1000,self.options)), item['content']))
        candidates.pack(fill='x')
        candidates.bind('<<TreeviewSelect>>', lambda e:bili.set(candidates.item(candidates.selection()[0], 'values')[0]) if candidates.selection() else None)
        ttk.Label(panel, text='例如：B 站删了 30 秒，10:00 的同一画面在本地为 10:30，后续弹幕就推迟 30 秒。\n'
                  '片头声音自动校准只核对开头，无法判断后面所有删减；分段调整可撤销。').pack(anchor='w', pady=10)
        feedback = tk.StringVar(value='已暂停电影。时间支持 00:10:30 或秒数。')
        ttk.Label(panel, textvariable=feedback, foreground='#58657c').pack(anchor='w')
        def apply():
            if not self.ready:
                feedback.set('请等待弹幕轨道更新完成。')
                return
            try:
                original, target = parse_time(bili.get()), parse_time(local.get())
                if target >= self.duration:
                    raise ValueError('本地时间需在电影时长以内。')
                self.replace_options(align_from(self.options, original, target))
            except ValueError as error:
                feedback.set(str(error))
                return
            dialog.destroy()
        actions = ttk.Frame(panel)
        actions.pack(fill='x', pady=(12,0))
        ttk.Button(actions, text='从这里开始对齐（可撤销）', command=apply).pack(side='right')
        ttk.Button(actions, text='取消', command=dialog.destroy).pack(side='right', padx=8)
        entry.focus_set()

    def auto_sync(self):
        self.link_sync()

    def link_sync(self):
        if not self.ready:
            return
        if self.sync_dialog and self.sync_dialog.winfo_exists():
            self.sync_dialog.lift()
            return
        dialog = self.sync_dialog = tk.Toplevel(self.window)
        dialog.title('从 B 站链接自动校准')
        dialog.transient(self.window)
        panel = ttk.Frame(dialog,padding=18);panel.pack(fill='both',expand=True)
        ttk.Label(panel,text='直接用当前 B 站链接匹配本地电影',
                  font=('Microsoft YaHei UI',12,'bold')).pack(anchor='w')
        ttk.Label(panel,text='自动分析正常可访问的参考音轨，无需提供文件或录音。\n只建议整体偏移；保留当前倍率和分段设置，核对后再保存。').pack(anchor='w',pady=8)
        text = tk.StringVar(value='点击开始，分析前最多 6 分钟的参考声音。接口暂不可用时可继续手动核对。')
        ttk.Label(panel,textvariable=text,wraplength=600).pack(anchor='w',pady=10)
        meter = ttk.Progressbar(panel,mode='indeterminate');meter.pack(fill='x')
        actions = ttk.Frame(panel);actions.pack(fill='x',pady=(12,0))
        result = {}
        def cancel():
            if self.sync_cancel:
                self.sync_cancel.set()
            dialog.destroy()
        dialog.protocol('WM_DELETE_WINDOW',cancel)
        def apply():
            if self.ready and result:
                if self.options != result['_options']:
                    text.set('同步设置已更改，请重新分析以使用最新设置。')
                    apply_button.configure(state='disabled')
                    return
                self.replace_options({**self.options,'offset':result['offset']})
                dialog.destroy()
                self.status.set('已预览参考片段的建议偏移；Ctrl+Z 可撤销，核对后再保存。')
        apply_button=ttk.Button(actions,text='应用并预览（可撤销）',command=apply,state='disabled')
        apply_button.pack(side='right')
        ttk.Button(actions,text='取消',command=cancel).pack(side='right',padx=6)
        def start():
            candidates=[shutil.which('ffmpeg'),str(Path(os.environ.get('APPDATA',''))/'.minecraft/ffmpeg/bin/ffmpeg.exe')]
            ffmpeg=next((p for p in candidates if p and Path(p).is_file()),None)
            if not ffmpeg:
                ffmpeg=filedialog.askopenfilename(parent=dialog,title='选择用于分析音轨的 ffmpeg.exe',filetypes=[('FFmpeg','ffmpeg.exe')])
            if not ffmpeg:
                return
            snapshot=json.loads(json.dumps(self.options))
            self.sync_cancel=token=threading.Event()
            messages=queue.Queue()
            start_button.configure(state='disabled');apply_button.configure(state='disabled');meter.start(15)
            def run():
                try:
                    from bili_sync import align_bilibili
                    values=align_bilibili(self.metadata,self.movie,ffmpeg,token,
                                          lambda s:messages.put(('progress',s)),snapshot)
                    messages.put(('result',values))
                except Exception as error:
                    messages.put(('error',str(error) or '已取消分析。'))
            threading.Thread(target=run,daemon=False).start()
            def poll_sync():
                if self.closed or not dialog.winfo_exists():
                    return
                ended=False
                while not messages.empty():
                    kind,value=messages.get_nowait()
                    if kind=='result':
                        result.clear();result.update(value);result['_options']=snapshot
                        text.set(f'参考 {value["reference_seconds"]:.0f} 秒，9 个时段中匹配 {len(value["matches"])} 个。\n建议整体偏移 {value["offset"]:+.2f} 秒，片段残差最大 {value["error"]:.2f} 秒。\n倍率和分段设置保持当前值；整片后续时段仍需核对。')
                        if self.options != snapshot:
                            result.clear()
                            text.set('分析期间同步设置已更改，请重新分析以使用最新设置。')
                        else:
                            apply_button.configure(state='normal')
                        ended=True
                    elif kind=='error':
                        text.set(value);ended=True
                    else:
                        text.set(value)
                if ended:
                    meter.stop();start_button.configure(state='normal')
                else:
                    dialog.after(100,poll_sync)
            dialog.after(100,poll_sync)
        start_button=ttk.Button(actions,text='开始自动匹配',command=start)
        start_button.pack(side='left')
        ttk.Button(panel,text='高级：已有两版文件时按声音匹配…',
                   command=lambda:(cancel(),self.file_sync())).pack(anchor='w',pady=(10,0))

    def file_sync(self):
        if not self.ready:
            return
        if self.sync_dialog and self.sync_dialog.winfo_exists():
            self.sync_dialog.lift()
            return
        dialog = self.sync_dialog = tk.Toplevel(self.window)
        dialog.title('按声音自动校准')
        dialog.transient(self.window)
        panel = ttk.Frame(dialog, padding=18)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='选择 B 站对应版本的视频或音频作为参考',
                  font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w')
        ttk.Label(panel, text='参考素材须从该版本 00:00 开始，且使用相同语言的音轨。\n工具会匹配多个时段的声音；结果先预览，满意后再保存。').pack(anchor='w', pady=8)
        reference = tk.StringVar()
        row = ttk.Frame(panel);row.pack(fill='x')
        ttk.Entry(row, textvariable=reference, width=62).pack(side='left', fill='x', expand=True)
        def choose():
            path = filedialog.askopenfilename(parent=dialog, title='选择参考版本的视频或音频',
                filetypes=[('视频或音频','*.mp4 *.mkv *.avi *.mov *.mp3 *.m4a *.aac *.wav *.flac'),('所有文件','*.*')])
            if path:
                reference.set(path)
        ttk.Button(row, text='选择…', command=choose).pack(side='left', padx=5)
        text = tk.StringVar(value='本地版本使用当前选择的电影。分析期间可以取消。')
        ttk.Label(panel, textvariable=text, wraplength=600).pack(anchor='w', pady=12)
        meter = ttk.Progressbar(panel, mode='indeterminate');meter.pack(fill='x')
        result = {}
        def cancel():
            if self.sync_cancel:
                self.sync_cancel.set()
            dialog.destroy()
        dialog.protocol('WM_DELETE_WINDOW', cancel)
        actions = ttk.Frame(panel);actions.pack(fill='x', pady=(12,0))
        def apply():
            if not self.ready:
                return
            values = {**self.options, 'rate':result['rate'], 'offset':result['offset'], 'segments':[]}
            self.replace_options(values)
            dialog.destroy()
            self.status.set('已预览声音校准结果；可撤销，核对后再保存。')
        apply_button = ttk.Button(actions, text='应用并预览（可撤销）', command=apply, state='disabled')
        apply_button.pack(side='right')
        ttk.Button(actions, text='取消', command=cancel).pack(side='right', padx=6)
        def start():
            filename = reference.get().strip()
            if not Path(filename).is_file():
                messagebox.showerror('选择参考文件','请先选择参考视频或音频。',parent=dialog);return
            candidates = [shutil.which('ffmpeg'), str(Path(os.environ.get('APPDATA',''))/'.minecraft/ffmpeg/bin/ffmpeg.exe')]
            ffmpeg = next((p for p in candidates if p and Path(p).is_file()), None)
            if not ffmpeg:
                ffmpeg = filedialog.askopenfilename(parent=dialog, title='选择用于分析音轨的 ffmpeg.exe', filetypes=[('FFmpeg','ffmpeg.exe')])
            if not ffmpeg:
                return
            self.sync_cancel = token = threading.Event()
            messages = queue.Queue()
            start_button.configure(state='disabled');apply_button.configure(state='disabled');meter.start(15)
            def run():
                try:
                    from audio_sync import align
                    values = align(filename, self.movie, ffmpeg, token, lambda s:messages.put(('progress',s)))
                    messages.put(('result',values))
                except Exception as error:
                    messages.put(('error',str(error) or '已取消分析。'))
            threading.Thread(target=run, daemon=False).start()
            def poll_sync():
                if self.closed or not dialog.winfo_exists():
                    return
                ended = False
                while not messages.empty():
                    kind, value = messages.get_nowait()
                    if kind == 'result':
                        result.clear();result.update(value)
                        text.set(f'9 个时段中匹配 {len(value["matches"])} 个，其中 {value["inliers"]} 个一致；最大残差 {value["error"]:.2f} 秒。\n建议整体偏移 {value["offset"]:+.2f} 秒，时间倍率 {value["rate"]:.6f}。\n应用将替换当前倍率、整体偏移和分段偏移；可撤销恢复。')
                        apply_button.configure(state='normal');ended=True
                    elif kind == 'error':
                        text.set(value);ended=True
                    else:
                        text.set(value)
                if ended:
                    meter.stop();start_button.configure(state='normal')
                else:
                    dialog.after(100,poll_sync)
            dialog.after(100,poll_sync)
        start_button = ttk.Button(actions,text='开始声音匹配',command=start)
        start_button.pack(side='left')

    def poll(self):
        if self.closed:
            return
        self.player.drain()
        down,pressed=self.native_mouse_sample()
        self.poll_fullscreen_controls()
        self.poll_movie_click(self.window.winfo_pointerxy(),down,pressed)
        self.loaded = self.player.number('duration') > 0
        if self.loaded:
            self.duration = self.player.number('duration', self.duration)
            if not self.scrubbing:
                self.position = self.player.number('time-pos', self.position)
                self.draw_progress()
        while not self.events.empty():
            event, value = self.events.get_nowait()
            if event == 'ass':
                ass, settings = value
                self.ass_serial += 1
                self.ass_path = Path(self.temporary.name)/f'danmaku_{self.ass_serial}.ass'
                self.ass_path.write_text(ass, encoding='utf-8-sig')
                self.pending_ass_options = settings
                self.pending_ass = True
            else:
                restore = getattr(self, 'pending_restore', None)
                if restore:
                    self.options, self.undo_stack, self.redo_stack = restore
                    self.pending_restore = None
                    self.ready = True
                    self.update_offset_label()
                    self.update_history_buttons()
                    self.draw_distribution()
                self.status.set('弹幕加载失败：'+value)
        if self.loaded and getattr(self, 'pending_ass', False):
            old_track = getattr(self, 'danmaku_track', None)
            if any(line.startswith('Dialogue:') for line in self.ass_path.read_text(encoding='utf-8-sig').splitlines()):
                self.player.command('sub-add', str(self.ass_path), 'select')
                self.danmaku_track = self.player.get('sid')
            else:
                self.danmaku_track = None
            if old_track and old_track not in ('no', 'auto'):
                self.player.command('sub-remove', old_track)
            self.base_offset = self.pending_ass_options['offset']
            self.player.set('sub-delay', 0)
            self.toggle_danmaku()
            self.pending_ass = False
            self.ready = True
            self.pending_restore = None
            self.update_history_buttons()
            self.status.set('已就绪 · 空格播放/暂停 · 拖动蓝色进度条跳转')
        self.load_ticks += 1
        if not self.loaded and self.load_ticks == 150:
            self.status.set('电影仍未打开，请检查文件是否可读或格式是否受支持。')
        self.window.after(80, self.poll)

    def close(self):
        if self.closed:
            return
        self.cancel_fast_forward()
        self.closed = True
        if self.sync_cancel:
            self.sync_cancel.set()
        if self.player:
            self.player.close()
        self.temporary.cleanup()
        self.window.destroy()
