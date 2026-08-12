@echo off
setlocal
cd /d "%~dp0"

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
set HF_HUB_CACHE=%ROOT%\HuggingFaceHub
set HY3DGEN_MODELS=%ROOT%\HuggingFaceHub
set HF_HUB_DISABLE_SYMLINKS_WARNING=1

rem Keep hub compatible with transformers (needs huggingface-hub^<1.0)
"%ROOT%\python_standalone\python.exe" -s -m pip install "huggingface-hub>=0.34.0,<1.0" "hf-xet" --quiet

echo.
echo === Shape model: Hunyuan3D-2mini turbo ===
"%ROOT%\python_standalone\Scripts\hf.exe" download "tencent/Hunyuan3D-2mini" --include "hunyuan3d-dit-v2-mini-turbo/*" --exclude "*.ckpt"
"%ROOT%\python_standalone\Scripts\hf.exe" download "tencent/Hunyuan3D-2mini" --include "hunyuan3d-vae-v2-mini-turbo/*" --exclude "*.ckpt"

echo.
echo === Text-to-image: HunyuanDiT distilled ===
"%ROOT%\python_standalone\Scripts\hf.exe" download "Tencent-Hunyuan/HunyuanDiT-v1.1-Diffusers-Distilled"

echo.
echo === Paint / texture: Hunyuan3D-2 (multiview + delight) ===
"%ROOT%\python_standalone\Scripts\hf.exe" download "tencent/Hunyuan3D-2" --include "hunyuan3d-paint-v2-0/*" --exclude "*.ckpt"
"%ROOT%\python_standalone\Scripts\hf.exe" download "tencent/Hunyuan3D-2" --include "hunyuan3d-delight-v2-0/*" --exclude "*.ckpt"

echo.
echo === sentencepiece (needed by HunyuanDiT) ===
"%ROOT%\python_standalone\python.exe" -s -m pip install sentencepiece

echo.
echo === Optional: build texgen CUDA extensions (required for paint) ===
echo If paint fails at runtime, from an elevated prompt in %%ROOT%%\Hunyuan3D-2 run:
echo   python_standalone\python.exe -s hy3dgen\texgen\custom_rasterizer\setup.py install
echo   python_standalone\python.exe -s hy3dgen\texgen\differentiable_renderer\setup.py install
echo.
echo Done. Hub cache: %HF_HUB_CACHE%
endlocal
