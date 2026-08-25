#!/bin/bash
# =============================================================================
# scripts/build_appimage.sh — CI-friendly AppImage builder for VaultPlay
#
# This is the machine-only counterpart to scripts/install_local.sh:
#   - No pacman, no sudo, no system icon-cache / .desktop registration
#   - Uses apt (Ubuntu runner) for the couple of native deps it needs
#   - Runs appimagetool via --appimage-extract instead of FUSE (GH Actions
#     runners don't reliably have /dev/fuse)
#   - Reads the version to embed from ./VERSION (written by the release
#     workflow from the git tag before this script runs)
#
# Output: VaultPlay-x86_64.AppImage at the repo root.
#
# Also runnable locally on any Linux box with apt or with the equivalent
# packages already installed — it never assumes Arch.
# =============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SRC_DIR="${REPO_DIR}/src"
BUILD_DIR="${REPO_DIR}/.appimage_build"
APPDIR="${BUILD_DIR}/AppDir"
APP_NAME="VaultPlay"
APP_ID="vaultplay"
APPIMAGE_NAME="${APP_NAME}-x86_64.AppImage"
VERSION="$(cat "${REPO_DIR}/VERSION" 2>/dev/null || echo "0.0.0-dev")"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[•]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }

echo ""
echo -e "${CYAN}  ⬡  VaultPlay AppImage Builder (CI)  —  v${VERSION}${NC}"
echo ""

# ── Native deps (best-effort; only installs what's missing) ─────────────────
if command -v apt-get &>/dev/null; then
    info "Installing build deps via apt..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq wget patchelf file >/dev/null
fi
success "Build deps OK."

# ── appimagetool (run via extracted AppRun — no FUSE needed) ────────────────
mkdir -p "$BUILD_DIR"
APPIMAGETOOL_AI="${BUILD_DIR}/appimagetool.AppImage"
if [ ! -f "$APPIMAGETOOL_AI" ]; then
    info "Downloading appimagetool..."
    wget -q "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage" \
        -O "$APPIMAGETOOL_AI"
    chmod +x "$APPIMAGETOOL_AI"
fi
if [ ! -d "${BUILD_DIR}/squashfs-root" ]; then
    (cd "$BUILD_DIR" && "$APPIMAGETOOL_AI" --appimage-extract >/dev/null)
fi
APPIMAGETOOL="${BUILD_DIR}/squashfs-root/AppRun"
success "appimagetool ready (extracted, FUSE-free)."

# ── Python + venv ────────────────────────────────────────────────────────────
info "Creating build virtual environment..."
python3 -m venv "${REPO_DIR}/.build_venv"
source "${REPO_DIR}/.build_venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet PyQt6 requests py7zr rarfile Pillow pyyaml beautifulsoup4
PYVER_SHORT="python$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
VENV_SITE="$(python3 -c "import site; print(site.getsitepackages()[0])")"
deactivate
success "Dependencies installed."

# ── AppDir structure ─────────────────────────────────────────────────────────
info "Setting up AppDir..."
mkdir -p "${APPDIR}/usr/bin/ui"
mkdir -p "${APPDIR}/usr/bin/assets"
mkdir -p "${APPDIR}/usr/lib/${PYVER_SHORT}/site-packages"
mkdir -p "${APPDIR}/usr/share/applications"
for SIZE in 16 32 48 128 256; do
    mkdir -p "${APPDIR}/usr/share/icons/hicolor/${SIZE}x${SIZE}/apps"
done

info "Copying app source from src/ ..."
cp "${SRC_DIR}/"*.py "${APPDIR}/usr/bin/"
cp "${SRC_DIR}/ui/"*.py "${APPDIR}/usr/bin/ui/"
cp "${REPO_DIR}/assets/"* "${APPDIR}/usr/bin/assets/" 2>/dev/null || true
# Bundled so settings_view.py's APP_VERSION can read it at runtime — see
# SettingsView._load_app_version() in src/ui/settings_view.py.
cp "${REPO_DIR}/VERSION" "${APPDIR}/usr/bin/VERSION"

for _tool in wrestool icotool; do
    _tool_path="$(command -v ${_tool} 2>/dev/null || true)"
    [ -n "${_tool_path}" ] && cp "${_tool_path}" "${APPDIR}/usr/bin/"
done
success "App source copied."

info "Bundling pip packages..."
cp -r "${VENV_SITE}/." "${APPDIR}/usr/lib/${PYVER_SHORT}/site-packages/"
success "Pip packages bundled."

info "Installing icons..."
ASSET_DIR="${REPO_DIR}/assets"
for SIZE in 16 32 48 128 256; do
    DEST="${APPDIR}/usr/share/icons/hicolor/${SIZE}x${SIZE}/apps/${APP_ID}.png"
    if [ -f "${ASSET_DIR}/icon_${SIZE}.png" ]; then
        cp "${ASSET_DIR}/icon_${SIZE}.png" "$DEST"
    elif [ -f "${ASSET_DIR}/icon.png" ]; then
        source "${REPO_DIR}/.build_venv/bin/activate" 2>/dev/null || true
        python3 -c "
from PIL import Image
Image.open('${ASSET_DIR}/icon.png').resize((${SIZE}, ${SIZE})).save('${DEST}')
" 2>/dev/null || cp "${ASSET_DIR}/icon.png" "$DEST"
    fi
done
[ -f "${ASSET_DIR}/icon.png" ] && cp "${ASSET_DIR}/icon.png" "${APPDIR}/${APP_ID}.png"
success "Icons installed."

info "Creating .desktop file..."
cat > "${APPDIR}/${APP_ID}.desktop" << DESKTOP
[Desktop Entry]
Name=${APP_NAME}
Comment=NAS Game Launcher — browse, manage and install your PC game library
Exec=${APP_ID}
Icon=${APP_ID}
Type=Application
Categories=Game;
Keywords=games;launcher;nas;wine;library;
StartupWMClass=VaultPlay
Terminal=false
DESKTOP
cp "${APPDIR}/${APP_ID}.desktop" "${APPDIR}/usr/share/applications/${APP_ID}.desktop"
success ".desktop file created."

info "Creating AppRun..."
cat > "${APPDIR}/AppRun" << 'APPRUN'
#!/bin/bash
APPDIR="$(dirname "$(readlink -f "${0}")")"
PYTHON="$(command -v python3)"
if [ -z "$PYTHON" ]; then
    echo "[VaultPlay] ERROR: python3 not found on this system." >&2
    exit 1
fi
PYVER="$($PYTHON -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
export PYTHONPATH="${APPDIR}/usr/bin:${APPDIR}/usr/lib/${PYVER}/site-packages:${PYTHONPATH}"
export PATH="${APPDIR}/usr/bin:${PATH}"
if [ -z "$QT_QPA_PLATFORM" ]; then
    export QT_QPA_PLATFORM=xcb
fi
cd "${APPDIR}/usr/bin"
exec "$PYTHON" "${APPDIR}/usr/bin/main.py" "$@"
APPRUN
chmod +x "${APPDIR}/AppRun"
success "AppRun created."

info "Building AppImage..."
rm -f "${REPO_DIR}/${APPIMAGE_NAME}"
ARCH=x86_64 "$APPIMAGETOOL" --no-appstream "${APPDIR}" "${REPO_DIR}/${APPIMAGE_NAME}" 2>&1 | grep -v "^$" || true

if [ ! -f "${REPO_DIR}/${APPIMAGE_NAME}" ]; then
    echo "ERROR: AppImage build failed — ${APPIMAGE_NAME} was not created." >&2
    exit 1
fi
chmod +x "${REPO_DIR}/${APPIMAGE_NAME}"
success "Built: ${APPIMAGE_NAME} ($(du -sh "${REPO_DIR}/${APPIMAGE_NAME}" | cut -f1))"

# ── Cleanup build scratch (keep the .AppImage + repo intact) ────────────────
rm -rf "$BUILD_DIR" "${REPO_DIR}/.build_venv"
success "Build scratch cleaned up."
