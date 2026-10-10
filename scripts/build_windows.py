"""Build from the repository root: python scripts/build_windows.py."""
import shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    subprocess.run([sys.executable,str(ROOT/'scripts/fetch_dependencies.py')],check=True,cwd=ROOT)
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--windowed','--onedir','--name','BiliDanmakuDesk',
        '--icon',str(ROOT/'assets/app.ico'),'--add-data',str(ROOT/'assets')+';assets',
        '--add-binary',str(ROOT/'vendor/DanmakuFactory.exe')+';vendor',
        '--add-binary',str(ROOT/'vendor/libmpv-2.dll')+';vendor',
        '--add-data',str(ROOT/'vendor/mpv-LICENSE.LGPL')+';vendor',
        '--add-data',str(ROOT/'vendor/DanmakuFactory-LICENSE.txt')+';vendor',str(ROOT/'app.py')],check=True,cwd=ROOT)
    dest=ROOT/'dist/BiliDanmakuDesk/licenses';dest.mkdir(exist_ok=True)
    for name in ('DanmakuFactory-LICENSE.txt','mpv-LICENSE.LGPL','mpv-COPYING.GPLv3','mpv-Copyright.txt','NumPy-LICENSE.txt','Python-LICENSE.txt','Tcl-LICENSE.txt','Tk-LICENSE.txt'):
        shutil.copy2(ROOT/'vendor'/name,dest/name)
    for p in (Path(sys.base_prefix)/'LICENSE.txt',):
        if p.exists():shutil.copy2(p,dest/'Python-LICENSE.txt')
    for family in ('tcl','tk'):
        for p in (Path(sys.base_prefix)/'tcl').glob(f'{family}*/license.terms'):
            shutil.copy2(p,dest/f'{family.title()}-LICENSE.txt')
    for name in ('README.md','LICENSE','AUTHORS.md','THIRD_PARTY.md'):
        shutil.copy2(ROOT/name,ROOT/'dist/BiliDanmakuDesk'/name)
    print('Built dist/BiliDanmakuDesk/BiliDanmakuDesk.exe')
if __name__=='__main__':main()
