"""Build independent, self-contained GUI-less Windows distribution.

The main EXE is windowed. A console-only stdio bridge EXE is also included so
MCP stdio pipes work correctly without making normal service launches visible.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
VERSION = '0.2.0'


def main():
    if sys.platform != 'win32': raise RuntimeError('Windows发行包须在Windows构建')
    sys.path.insert(0,str(ROOT/'windows'))
    from tools.prepare_runtime import prepare
    runtime = prepare()
    for name,windowed in [('OmniDownloaderAgent',True),('OmniDownloaderAgentMCP',False)]:
        command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir',
            '--name',name,'--distpath',str(BASE/'dist'),
            '--workpath',str(BASE/'build'/name),'--specpath',str(BASE/'build'/name),
            '--paths',str(BASE),'--paths',str(ROOT/'windows'),
            '--add-data',f'{ROOT / "windows" / "core"};windows/core',
            '--add-data',f'{ROOT / "windows" / "engine"};windows/engine',
            '--add-data',f'{runtime};bin',
            '--add-data',f'{BASE / "omni_agent" / "static"};omni_agent/static',
            '--add-data',f'{ROOT / "windows" / "app.ico"};.',
            '--icon',str(ROOT/'windows'/'app.ico'),
            '--collect-submodules','mcp.server','--collect-submodules','mcp.client',
            '--collect-all','mcp_types','--collect-all','yt_dlp',
            '--collect-all','yt_dlp_ejs','--collect-all','cryptography','--collect-all','imageio_ffmpeg',
            '--collect-all','gallery_dl','--collect-all','qrcode',
            '--collect-submodules','engine','--collect-submodules','core',
            '--hidden-import','uvicorn.logging','--hidden-import','uvicorn.loops.auto',
            '--hidden-import','uvicorn.protocols.http.auto','--hidden-import','uvicorn.protocols.websockets.auto',
            '--hidden-import','pystray._win32','--hidden-import','PIL._tkinter_finder',
            '--hidden-import','tkinter','--hidden-import','tkinter.filedialog',
            '--hidden-import','websockets',
            '--copy-metadata','mcp','--copy-metadata','mcp-types','--copy-metadata','yt-dlp','--copy-metadata','yt-dlp-ejs']
        if windowed:command+=['--windowed']
        command += [str(BASE/'launcher.py')]
        subprocess.run(command,cwd=BASE,check=True)
    output=BASE/'dist'/'OmniDownloaderAgent'
    bridge=BASE/'dist'/'OmniDownloaderAgentMCP'
    # Same dependencies; retain console bootloader alongside the windowed EXE.
    shutil.copy2(bridge/'OmniDownloaderAgentMCP.exe',output/'OmniDownloaderAgentMCP.exe')
    for source in [BASE/'README.md',BASE/'requirements.lock',ROOT/'LICENSE',ROOT/'windows'/'runtime-lock.json']:
        shutil.copy2(source,output/source.name)
    shutil.copy2(ROOT/'UPSTREAM.md',output/'UPSTREAM.md')
    shutil.copytree(BASE/'docs',output/'docs',dirs_exist_ok=True)
    shutil.copytree(BASE/'examples',output/'examples',dirs_exist_ok=True)
    shutil.copytree(ROOT/'windows'/'licenses',output/'licenses',dirs_exist_ok=True)
    (output/'THIRD_PARTY_NOTICES.md').write_text(
        '# Components\n\nOmniDownloader core: MIT (LICENSE).\nFFmpeg 7.1 Gyan GPL v3 build; license included. Source and exact runtime hashes: runtime-lock.json and https://github.com/GyanD/codexffmpeg/releases/tag/7.1 .\n'
        'Deno 2.9.8: MIT, license included; https://github.com/denoland/deno/tree/v2.9.8 .\n'
        'qrcode / websockets: open-source licenses included where supplied by packages.\n'
        'This distribution does not ship pywebview or any embedded browser binary; local desktop Chrome/Edge login uses the user-installed browser in an isolated profile.\n'
        'Python component versions and hashes: requirements.lock. Installed package license/metadata files are included where supplied by their packages.\n',encoding='utf-8')
    (output/'使用说明.txt').write_text(
        '双击 OmniDownloaderAgent.exe 启动后台服务。首次自动打开 http://127.0.0.1:58080/ 。\n'
        '本版本已取消令牌授权设定，用户与 Agent 可免令牌本机直连并直接管理设置。\n'
        '工作与数据目录优先使用独立工作区根目录下的 data/（独立便携复制时使用同级 data/），不再写入 %LOCALAPPDATA%。\n'
        '支持在管理页选择文件夹与保存目录、测试网络代理连通性、配置下载阶段可视化通知与自动识别已接入桥接。\n'
        '平台 Cookie 支持官方扫码、拉起本机 Chrome/Edge 独立窗口登录自动捕获及多格式加密导入；Agent 仅可查看非敏感状态、禁用与移除 Cookie。\n'
        '右下角托盘菜单可打开管理页、开关“开机自启动”（登录后静默托盘运行不自动弹浏览器）或安全退出。MCP stdio 请使用 OmniDownloaderAgentMCP.exe --stdio 。\n'
        '无需安装 Python 或 FFmpeg。未提供平台测试账号，平台登录和真实发送请按 docs/OPERATIONS.md 验证。\n',encoding='utf-8')
    release=BASE/'dist'/f'OmniDownloader-Agent-Edition-{VERSION}-windows-x64.zip'
    with zipfile.ZipFile(release,'w',zipfile.ZIP_DEFLATED) as archive:
        for file in output.rglob('*'):
            if file.is_file(): archive.write(file,str(file.relative_to(output.parent)))
    with release.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    release.with_suffix('.sha256').write_text(f'{digest}  {release.name}\n')
    print(json.dumps({'artifact':str(release),'sha256':digest}))


if __name__=='__main__':main()
