# PyInstaller spec for Linotype.app. Build with ./build_app.sh
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = collect_all("pymupdf")
hiddenimports += collect_submodules("fontTools") + collect_submodules("webview")

a = Analysis(["desktop.py"], datas=datas, binaries=binaries, hiddenimports=hiddenimports,
             excludes=["playwright", "tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Linotype", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Linotype")
app = BUNDLE(
    coll,
    name="Linotype.app",
    icon="assets/icon.icns",
    bundle_identifier="app.linotype.desktop",
    version="0.1.0",
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        # offer Linotype under Finder's "Open With" for PDFs, without taking over the default
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "PDF Document",
            "CFBundleTypeRole": "Editor",
            "LSHandlerRank": "Alternate",
            "LSItemContentTypes": ["com.adobe.pdf"],
        }],
    },
)
