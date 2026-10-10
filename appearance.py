# coding: utf-8
# Copyright (c) 2026 霜星. All rights reserved.
"""Shared display settings for the main window and movie preview."""
import json
import tkinter as tk
from tkinter import ttk, font as tkfont
from core import validate_options


def display_dialog(parent, options, on_apply):
    dialog = tk.Toplevel(parent)
    dialog.title('弹幕显示设置')
    dialog.transient(parent)
    panel = ttk.Frame(dialog, padding=16)
    panel.pack(fill='both', expand=True)
    families = sorted(set(name for name in tkfont.families(parent) if not name.startswith('@')))
    font = tk.StringVar(value=options.get('font','Microsoft YaHei'))
    ttk.Label(panel, text='弹幕字体').grid(row=0,column=0,sticky='w')
    ttk.Combobox(panel,textvariable=font,values=families,state='readonly',width=32).grid(row=0,column=1,sticky='ew',pady=6)
    size=tk.StringVar(value=str(options['size']))
    area=tk.StringVar(value=f"{options['area']*100:g}")
    ttk.Label(panel,text='弹幕字号').grid(row=1,column=0,sticky='w')
    ttk.Spinbox(panel,from_=20,to=64,increment=2,textvariable=size,width=10).grid(row=1,column=1,sticky='w',pady=6)
    ttk.Label(panel,text='显示区域 %').grid(row=2,column=0,sticky='w')
    ttk.Spinbox(panel,from_=20,to=100,increment=5,textvariable=area,width=10).grid(row=2,column=1,sticky='w',pady=6)
    ttk.Label(panel,text='弹幕使用画面上方的多少百分比：50% 为上半屏，100% 为全屏。').grid(row=3,column=0,columnspan=2,sticky='w',pady=4)
    opacity=tk.StringVar(value=str(options['opacity']))
    ttk.Label(panel,text='弹幕不透明度 %').grid(row=4,column=0,sticky='w')
    opacity_controls=ttk.Frame(panel)
    opacity_controls.grid(row=4,column=1,sticky='ew',pady=6)
    ttk.Spinbox(opacity_controls,from_=1,to=100,increment=5,textvariable=opacity,width=8).pack(side='left')
    ttk.Scale(opacity_controls,from_=1,to=100,variable=opacity,length=190,
              command=lambda value:opacity.set(str(round(float(value))))).pack(side='left',padx=10)
    ttk.Label(panel,text='数值越小越透明；100% 完全不透明。').grid(row=5,column=0,columnspan=2,sticky='w',pady=4)
    ttk.Label(panel,text='滚过屏幕所需秒数').grid(row=6,column=0,sticky='w',padx=(0,12))
    travel = tk.StringVar(value=str(options['travel']))
    ttk.Spinbox(panel,from_=3,to=20,increment=.5,textvariable=travel,width=10).grid(row=6,column=1,sticky='w',pady=6)
    ttk.Label(panel,text='3–20 秒；数值越小越快。只改变滚动速度，不改变出现时间。').grid(row=7,column=0,columnspan=2,sticky='w',pady=4)
    ttk.Label(panel,text='屏蔽词（每行一个，包含任意词的整条弹幕隐藏，英文不区分大小写）').grid(row=8,column=0,columnspan=2,sticky='w',pady=(12,4))
    words = tk.Text(panel,height=6,width=65,font=('Microsoft YaHei UI',10))
    words.grid(row=9,column=0,columnspan=2,sticky='ew')
    words.insert('1.0','\n'.join(options.get('block_words',[])))
    ttk.Label(panel,text='删除屏蔽词即可恢复。原始 XML 和项目中的原始弹幕始终保留。').grid(row=10,column=0,columnspan=2,sticky='w',pady=8)
    feedback = tk.StringVar()
    ttk.Label(panel,textvariable=feedback,foreground='#58657c').grid(row=11,column=0,columnspan=2,sticky='w')
    def apply():
        try:
            updated=json.loads(json.dumps(options))
            updated['font']=font.get()
            updated['size']=int(size.get())
            updated['area']=float(area.get())/100
            updated['opacity']=round(float(opacity.get()))
            updated['travel']=float(travel.get())
            updated['block_words']=list(dict.fromkeys(word.strip() for word in words.get('1.0','end').splitlines() if word.strip()))
            validate_options(updated)
            if on_apply(updated) is False:
                feedback.set('请等待弹幕轨道更新后再应用。')
                return
        except ValueError as error:
            feedback.set(str(error));return
        dialog.destroy()
    actions=ttk.Frame(panel)
    actions.grid(row=12,column=0,columnspan=2,sticky='e',pady=(8,0))
    ttk.Button(actions,text='取消',command=dialog.destroy).pack(side='left',padx=8)
    ttk.Button(actions,text='应用设置',command=apply).pack(side='left')
    # Modal settings prevent a stale snapshot from overwriting concurrent timing edits.
    dialog.grab_set()
    return dialog
