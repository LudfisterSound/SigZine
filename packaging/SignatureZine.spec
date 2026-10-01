# PyInstaller spec for Signature Zine.      -*- mode: python ; coding: utf-8 -*-
#
# Build with packaging/build.sh rather than by hand, so the icon exists
# first. One spec covers all three platforms; the macOS .app comes from the
# BUNDLE block at the bottom, which PyInstaller ignores elsewhere.
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
sys.path.insert(0, str(ROOT))
from sigzine import APP_NAME, __version__       # noqa: E402

# Qt is enormous and most of it is never touched. Leaving the heavy unused
# modules out takes the build from roughly 400 MB to well under half that,
# and a module listed here that turns out to be needed fails loudly at
# startup rather than subtly, so this list is safe to prune further.
UNUSED_QT = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtNfc", "PySide6.QtOpcUa",
    "PySide6.QtPositioning", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtTest", "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets", "PySide6.QtWebView",
]

EXCLUDES = UNUSED_QT + [
    "tkinter", "unittest", "pydoc", "doctest", "setuptools", "pip",
    "matplotlib", "scipy", "pandas", "IPython",
]

icon_mac = SPEC_DIR / "icon.icns"
icon_win = SPEC_DIR / "icon.ico"
icon = str(icon_mac) if sys.platform == "darwin" and icon_mac.exists() else \
       (str(icon_win) if icon_win.exists() else None)

datas = []
sample = ROOT / "2nd birthday.sigzine"
if sample.exists():
    datas.append((str(sample), "samples"))
icon_png = SPEC_DIR / "icons" / "icon.png"
if icon_png.exists():
    datas.append((str(icon_png), "."))

a = Analysis(
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    # PyMuPDF and Pillow both pull plugins in at runtime by name, which a
    # static scan cannot see.
    hiddenimports=["pymupdf", "fitz", "PIL._tkinter_finder"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Signature Zine" if sys.platform == "darwin" else "signature-zine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # a GUI app: no terminal window
    disable_windowed_traceback=False,
    argv_emulation=sys.platform == "darwin",   # lets Finder pass dropped files
    target_arch=None,       # build.sh sets this for a universal2 build
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Signature Zine" if sys.platform == "darwin" else "signature-zine",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="net.sigzine.SignatureZine",
        version=__version__,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            # Qt draws its own window chrome; without this the app ignores
            # the system light/dark setting.
            "NSRequiresAquaSystemAppearance": False,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.graphics-design",
            # So a .sigzine file opens this app when double-clicked.
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Signature Zine document",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Owner",
                "LSItemContentTypes": ["net.sigzine.document"],
                "CFBundleTypeExtensions": ["sigzine"],
                "CFBundleTypeIconFile": "icon.icns",
            }],
            "UTExportedTypeDeclarations": [{
                "UTTypeIdentifier": "net.sigzine.document",
                "UTTypeDescription": "Signature Zine document",
                "UTTypeConformsTo": ["public.data", "public.content"],
                "UTTypeTagSpecification": {
                    "public.filename-extension": ["sigzine"],
                },
            }],
        },
    )
