@echo off
setlocal
cd /d "%~dp0"

rem Heavy Hunyuan WinPortable + HF weights live outside the git repo.
rem Override with BUILDPLATE_HY3D_ROOT if you install elsewhere.
if not defined BUILDPLATE_HY3D_ROOT (
  set "BUILDPLATE_HY3D_ROOT=C:\buildplate-worker\Hunyuan3D2_WinPortable\Hunyuan3D2_WinPortable"
)

set "ROOT=%BUILDPLATE_HY3D_ROOT%"
if not exist "%ROOT%\python_standalone\python.exe" (
  echo ERROR: Hunyuan runtime not found at:
  echo   %ROOT%
  echo Set BUILDPLATE_HY3D_ROOT or install WinPortable under C:\buildplate-worker\
  exit /b 1
)

set PATH=%PATH%;%ROOT%\MinGit\cmd;%ROOT%\python_standalone\Scripts
rem Torch CUDA extensions need torch\lib + CUDA bin on PATH (custom_rasterizer_kernel).
set "TORCH_LIB=%ROOT%\python_standalone\Lib\site-packages\torch\lib"
if exist "%TORCH_LIB%" set "PATH=%TORCH_LIB%;%PATH%"
if defined CUDA_HOME (
  set "PATH=%CUDA_HOME%\bin;%PATH%"
) else if exist "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin" (
  set "CUDA_HOME=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9"
  set "PATH=%CUDA_HOME%\bin;%PATH%"
)
set PYTHONPATH=%ROOT%\Hunyuan3D-2;%PYTHONPATH%
set PYTHONPYCACHEPREFIX=%ROOT%\pycache
set HF_HUB_CACHE=%ROOT%\HuggingFaceHub
set HY3DGEN_MODELS=%ROOT%\HuggingFaceHub
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set HF_HUB_DISABLE_SYMLINKS=1
set BUILDPLATE_CACHE=%~dp0cache
set BUILDPLATE_HY3D_ROOT=%ROOT%

if exist "%~dp0.worker_secret" (
  for /f "usebackq delims=" %%A in ("%~dp0.worker_secret") do set "WORKER_SECRET=%%A"
)

if not exist "%BUILDPLATE_CACHE%" mkdir "%BUILDPLATE_CACHE%"
set "START_LOG=%BUILDPLATE_CACHE%\start-worker.out.log"
>>"%START_LOG%" echo [%date% %time%] start-worker.bat begin ROOT=%ROOT%
rem trimesh simplify_quadric_decimation needs this (paint UV unwrap safety).
"%ROOT%\python_standalone\python.exe" -s -m pip install --disable-pip-version-check -q fast_simplification 2>nul
cd /d "%ROOT%\Hunyuan3D-2"
echo Starting Buildplate worker on http://0.0.0.0:8081 ...
echo Health: http://127.0.0.1:8081/health
echo Logs:   %BUILDPLATE_CACHE%\worker.log
echo Mode: localhost only (no tunnel)
rem --verbose → DEBUG to console + worker\log file. Remote: POST /v1/admin/update
"%ROOT%\python_standalone\python.exe" -s "%~dp0buildplate_worker.py" --host 0.0.0.0 --port 8081 --enable_t23d --enable_texgen --verbose
set "EC=%ERRORLEVEL%"
>>"%START_LOG%" echo [%date% %time%] worker exited code=%EC%
echo Worker exited with code %EC%
if /i not "%BUILDPLATE_WORKER_NOPAUSE%"=="1" pause
exit /b %EC%
