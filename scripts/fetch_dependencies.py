"""Fetch the byte-verified, unmodified LGPL runtime used by this release."""
import hashlib,shutil,tempfile,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
URL='https://github.com/sx050907-png/BiliDanmakuDesk/releases/download/v0.13.0/libmpv-lgpl-pinned.zip'
ARCHIVE_SHA256='f310c39054cdef39715f6debeeb04ad4f3acdee0cee72dec67564ec6efd4ea88'
DLL_SHA256='5c595cc00f839b2f2fba395d80614d80ab938ec03a47f96ad39c18dd0e26355c'
def digest(path):
    with path.open('rb') as f:return hashlib.sha256(f.read()).hexdigest()
def main():
    target=ROOT/'vendor/libmpv-2.dll'
    if target.exists():
        if digest(target)!=DLL_SHA256:raise RuntimeError('Existing DLL differs from the pinned release. Back it up before rebuilding.')
        print('Verified existing libmpv runtime');return
    with tempfile.TemporaryDirectory(prefix='bilidanmaku-deps-') as folder:
        archive=Path(folder)/'runtime.zip'
        request=urllib.request.Request(URL,headers={'User-Agent':'BiliDanmakuDesk'})
        with urllib.request.urlopen(request,timeout=120) as response,archive.open('wb') as out:shutil.copyfileobj(response,out)
        if digest(archive)!=ARCHIVE_SHA256:raise RuntimeError('Runtime archive checksum mismatch')
        with zipfile.ZipFile(archive) as z:
            data=z.read('libmpv-2.dll')
        if hashlib.sha256(data).hexdigest()!=DLL_SHA256:raise RuntimeError('Runtime DLL checksum mismatch')
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
    print('Downloaded and verified libmpv runtime')
if __name__=='__main__':main()
