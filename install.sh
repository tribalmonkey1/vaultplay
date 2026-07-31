#!/bin/bash
# =============================================================================
# VaultPlay — AppImage builder for Arch Linux
# Builds a self-contained AppImage, installs it to ~/Applications,
# installs the icon, and creates a .desktop entry.
# Cleans up all intermediate build files when done.
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="VaultPlay"
APP_ID="vaultplay"
APPIMAGE_NAME="${APP_NAME}-x86_64.AppImage"
BUILD_DIR="${SCRIPT_DIR}/.appimage_build"
APPDIR="${BUILD_DIR}/AppDir"
INSTALL_DIR="$HOME/Applications"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[•]${NC} $*"; }
success() { echo -e "${GREEN}[✓]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
error()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }

cleanup() {
    if [ -d "$BUILD_DIR" ]; then
        info "Cleaning up build directory..."
        rm -rf "$BUILD_DIR"
        success "Build directory removed."
    fi
    if [ -d "${SCRIPT_DIR}/.build_venv" ]; then
        rm -rf "${SCRIPT_DIR}/.build_venv"
    fi
}
trap cleanup EXIT

echo ""
echo -e "${CYAN}  ⬡  VaultPlay AppImage Builder${NC}"
echo    "  ──────────────────────────────────────────"
echo ""

# =============================================================================
# STEP 1 — Check Python
# =============================================================================
info "Checking Python version..."
if ! command -v python3 &>/dev/null; then
    error "Python 3 not found. Install with: sudo pacman -S python"
fi
PYTHON_BIN="$(command -v python3)"
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYMINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
PYVER_SHORT="python${PYVER}"
if [ "$PYMINOR" -lt 10 ]; then
    error "Python 3.10+ required. Found Python ${PYVER}."
fi
success "Python ${PYVER} found at ${PYTHON_BIN}."

# =============================================================================
# STEP 2 — Check/install build dependencies
# =============================================================================
info "Checking build dependencies..."
MISSING=()
for cmd in wget patchelf; do
    command -v "$cmd" &>/dev/null || MISSING+=("$cmd")
done
if ! pacman -Qq fuse2 &>/dev/null 2>&1 && ! pacman -Qq fuse &>/dev/null 2>&1; then
    MISSING+=("fuse2")
fi
if [ ${#MISSING[@]} -gt 0 ]; then
    warn "Missing build dependencies: ${MISSING[*]}"
    read -rp "  Install them now with pacman? [Y/n] " confirm
    if [[ ! "$confirm" =~ ^[Nn]$ ]]; then
        sudo pacman -S --needed "${MISSING[@]}"
    else
        error "Cannot build without: ${MISSING[*]}"
    fi
fi
success "Build dependencies OK."

# =============================================================================
# STEP 3 — Download appimagetool
# =============================================================================
APPIMAGETOOL="${BUILD_DIR}/appimagetool"
mkdir -p "$BUILD_DIR"

if command -v appimagetool &>/dev/null; then
    APPIMAGETOOL="$(command -v appimagetool)"
    success "appimagetool found at ${APPIMAGETOOL}."
else
    info "Downloading appimagetool..."
    wget -q --show-progress \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage" \
        -O "$APPIMAGETOOL"
    chmod +x "$APPIMAGETOOL"
    success "appimagetool downloaded."
fi

# =============================================================================
# STEP 4 — Create build venv and install pip packages
# =============================================================================
info "Creating build virtual environment..."
python3 -m venv "${SCRIPT_DIR}/.build_venv"
source "${SCRIPT_DIR}/.build_venv/bin/activate"

info "Installing app dependencies into build venv..."
pip install --quiet --upgrade pip
pip install --quiet PyQt6 requests py7zr rarfile Pillow pyyaml beautifulsoup4
VENV_SITE=$(python3 -c "import site; print(site.getsitepackages()[0])")
success "Dependencies installed. Site-packages: ${VENV_SITE}"

# Deactivate — we'll reference the venv site-packages directly from now on
deactivate

# =============================================================================
# STEP 5 — Build AppDir structure
# =============================================================================
info "Setting up AppDir..."
mkdir -p "${APPDIR}/usr/bin/ui"
mkdir -p "${APPDIR}/usr/bin/assets"
mkdir -p "${APPDIR}/usr/lib/${PYVER_SHORT}/site-packages"
mkdir -p "${APPDIR}/usr/share/applications"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/16x16/apps"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/32x32/apps"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/48x48/apps"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/128x128/apps"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/256x256/apps"

# Copy app source files
info "Copying app source files..."
cp "${SCRIPT_DIR}/main.py"      "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/db.py"        "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/scanner.py"   "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/metadata.py"  "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/installer.py" "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/protondb.py"  "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/steamdb.py"   "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/redists.py"   "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/version_check.py" "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/version_checker.py" "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/playtime.py"      "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/save_backup.py"   "${APPDIR}/usr/bin/"
cp "${SCRIPT_DIR}/ui/"*.py      "${APPDIR}/usr/bin/ui/"
cp "${SCRIPT_DIR}/assets/"*     "${APPDIR}/usr/bin/assets/" 2>/dev/null || true

# Bundle icoutils (wrestool + icotool) for .exe icon extraction.
# These are standard system binaries — copy them directly if available.
# If not installed on the build machine, icon extraction will silently
# fall back to SGDB cover art at install time (no crash, just no exe icon).
for _tool in wrestool icotool; do
    _tool_path="$(command -v ${_tool} 2>/dev/null || true)"
    if [ -n "${_tool_path}" ]; then
        cp "${_tool_path}" "${APPDIR}/usr/bin/"
        success "Bundled ${_tool} from ${_tool_path}"
    else
        warn "icoutils: ${_tool} not found on build machine — exe icon extraction will not be available in this build. Install icoutils and rebuild to enable it."
    fi
done
success "App source files copied."

# =============================================================================
# STEP 6 — Copy pip packages into AppDir
# =============================================================================
info "Bundling pip packages into AppDir..."
APPDIR_SITE="${APPDIR}/usr/lib/${PYVER_SHORT}/site-packages"

# Copy all installed packages from the build venv
cp -r "${VENV_SITE}/." "${APPDIR_SITE}/"
success "Pip packages bundled."

# =============================================================================
# STEP 7 — Install icons
# =============================================================================
info "Installing icons..."
ASSET_DIR="${SCRIPT_DIR}/assets"
for SIZE in 16 32 48 128 256; do
    ICON_DEST="${APPDIR}/usr/share/icons/hicolor/${SIZE}x${SIZE}/apps"
    if [ -f "${ASSET_DIR}/icon_${SIZE}.png" ]; then
        cp "${ASSET_DIR}/icon_${SIZE}.png" "${ICON_DEST}/${APP_ID}.png"
    elif [ -f "${ASSET_DIR}/icon.png" ]; then
        python3 -c "
from PIL import Image
img = Image.open('${ASSET_DIR}/icon.png').resize((${SIZE}, ${SIZE}), Image.LANCZOS)
img.save('${ICON_DEST}/${APP_ID}.png')
"
    fi
done
# AppImage spec requires icon at root of AppDir
[ -f "${ASSET_DIR}/icon.png" ] && cp "${ASSET_DIR}/icon.png" "${APPDIR}/${APP_ID}.png"
success "Icons installed."

# =============================================================================
# STEP 8 — Create .desktop file
# =============================================================================
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
cp "${APPDIR}/${APP_ID}.desktop" \
   "${APPDIR}/usr/share/applications/${APP_ID}.desktop"
success ".desktop file created."

# =============================================================================
# STEP 9 — Create AppRun
# Uses the system Python3 (already confirmed 3.10+ above).
# Adds the bundled site-packages to PYTHONPATH so PyQt6 etc. are found.
# =============================================================================
info "Creating AppRun entrypoint..."
cat > "${APPDIR}/AppRun" << APPRUN
#!/bin/bash
APPDIR="\$(dirname "\$(readlink -f "\${0}")")"

# Use system Python — confirmed 3.10+ by the build script
PYTHON="\$(command -v python3)"
if [ -z "\$PYTHON" ]; then
    echo "[VaultPlay] ERROR: python3 not found on this system." >&2
    echo "[VaultPlay] Install Python 3.10+ and re-run the AppImage." >&2
    exit 1
fi

# Set PYVER for site-packages path
PYVER="\$(\$PYTHON -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"

# PYTHONPATH: app source dir FIRST (so debug.py, db.py etc. are always found),
# then bundled site-packages (PyQt6, requests etc.)
export PYTHONPATH="\${APPDIR}/usr/bin:\${APPDIR}/usr/lib/\${PYVER}/site-packages:\${PYTHONPATH}"

# Prepend AppDir bin to PATH so bundled tools (wrestool, icotool) are
# found before any system versions.
export PATH="\${APPDIR}/usr/bin:\${PATH}"

# Qt platform — xcb works on KDE Plasma and Hyprland.
# Override with QT_QPA_PLATFORM=wayland for native Wayland.
if [ -z "\$QT_QPA_PLATFORM" ]; then
    export QT_QPA_PLATFORM=xcb
fi

cd "\${APPDIR}/usr/bin"
exec "\$PYTHON" "\${APPDIR}/usr/bin/main.py" "\$@"
APPRUN
chmod +x "${APPDIR}/AppRun"
success "AppRun created (using system Python at ${PYTHON_BIN})."

# =============================================================================
# STEP 10 — Build the AppImage
# =============================================================================
info "Building AppImage..."
ARCH=x86_64 "$APPIMAGETOOL" \
    --no-appstream \
    "${APPDIR}" \
    "${SCRIPT_DIR}/${APPIMAGE_NAME}" 2>&1 | grep -v "^$" || true

if [ ! -f "${SCRIPT_DIR}/${APPIMAGE_NAME}" ]; then
    error "AppImage build failed — ${APPIMAGE_NAME} was not created."
fi
chmod +x "${SCRIPT_DIR}/${APPIMAGE_NAME}"
APPIMAGE_SIZE=$(du -sh "${SCRIPT_DIR}/${APPIMAGE_NAME}" | cut -f1)
success "AppImage built: ${APPIMAGE_NAME} (${APPIMAGE_SIZE})"

# =============================================================================
# STEP 11 — Install AppImage to ~/Applications
# =============================================================================
info "Installing AppImage to ~/Applications..."
mkdir -p "$INSTALL_DIR"
rm -f "${INSTALL_DIR}/${APPIMAGE_NAME}"
cp "${SCRIPT_DIR}/${APPIMAGE_NAME}" "${INSTALL_DIR}/${APPIMAGE_NAME}"
chmod +x "${INSTALL_DIR}/${APPIMAGE_NAME}"
success "AppImage installed to ${INSTALL_DIR}/${APPIMAGE_NAME}"

# =============================================================================
# STEP 12 — Install icons to system icon theme
# =============================================================================
info "Installing icons to system icon theme..."
ICON_BASE="$HOME/.local/share/icons/hicolor"
for SIZE in 16 32 48 128 256; do
    ICON_DEST="${ICON_BASE}/${SIZE}x${SIZE}/apps"
    mkdir -p "$ICON_DEST"
    if [ -f "${ASSET_DIR}/icon_${SIZE}.png" ]; then
        cp "${ASSET_DIR}/icon_${SIZE}.png" "${ICON_DEST}/${APP_ID}.png"
    elif [ -f "${ASSET_DIR}/icon.png" ]; then
        python3 -c "
from PIL import Image
img = Image.open('${ASSET_DIR}/icon.png').resize((${SIZE}, ${SIZE}), Image.LANCZOS)
img.save('${ICON_DEST}/${APP_ID}.png')
"
    fi
done
# Refresh icon caches
command -v gtk-update-icon-cache &>/dev/null && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v kbuildsycoca6 &>/dev/null && kbuildsycoca6 2>/dev/null || true
command -v kbuildsycoca5 &>/dev/null && kbuildsycoca5 2>/dev/null || true
success "Icons installed to ~/.local/share/icons/hicolor/"

# =============================================================================
# STEP 13 — Create .desktop entry
# =============================================================================
info "Creating .desktop entry..."
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
rm -f "${DESKTOP_DIR}/${APP_ID}.desktop"

cat > "${DESKTOP_DIR}/${APP_ID}.desktop" << DESKTOP
[Desktop Entry]
Name=${APP_NAME}
Comment=NAS Game Launcher — browse, manage and install your PC game library
Exec=${INSTALL_DIR}/${APPIMAGE_NAME} %U
Icon=${APP_ID}
Type=Application
Categories=Game;
Keywords=games;launcher;nas;wine;library;
StartupWMClass=VaultPlay
Terminal=false
DESKTOP

command -v desktop-file-validate &>/dev/null && \
    desktop-file-validate "${DESKTOP_DIR}/${APP_ID}.desktop" 2>/dev/null || true
command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
command -v xdg-desktop-menu &>/dev/null && \
    xdg-desktop-menu forceupdate 2>/dev/null || true
success ".desktop entry created at ${DESKTOP_DIR}/${APP_ID}.desktop"

# =============================================================================
# Done — cleanup runs automatically via trap
# =============================================================================
echo ""
echo -e "${GREEN}  ✓  VaultPlay installed successfully!${NC}"
echo ""
printf "  %-14s %s\n" "AppImage:"  "${INSTALL_DIR}/${APPIMAGE_NAME}"
printf "  %-14s %s\n" "Desktop:"   "${DESKTOP_DIR}/${APP_ID}.desktop"
printf "  %-14s %s\n" "Icons:"     "~/.local/share/icons/hicolor/*/apps/${APP_ID}.png"
printf "  %-14s %s\n" "Python:"    "${PYTHON_BIN} (${PYVER})"
echo ""
echo    "  Launch from your app menu, or run:"
echo -e "  ${CYAN}  ${INSTALL_DIR}/${APPIMAGE_NAME}${NC}"
echo ""
echo    "  First run:"
echo    "    1. Settings → NAS Connection → enter your NAS path"
echo    "    2. Settings → API Keys → add your SteamGridDB key"
echo    "    3. The library will scan and populate automatically"
echo ""
