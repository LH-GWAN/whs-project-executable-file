from __future__ import annotations

import io
import sys


def _ensure_std_streams() -> None:
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, io.TextIOWrapper(
                io.BytesIO(), encoding="utf-8", errors="replace", write_through=True))


_ensure_std_streams()


def _diagnose() -> int:
    import os

    from core.paths import is_frozen, resource_root, vendor_dir
    from core.appconfig import (
        MAP_SERVER_PREFERRED_PORTS, get_map_mode, keys_file_path, mask_key, online_keys,
    )
    from core.basemap import basemap_path, extract_zipped_basemap
    from ui.map_server import vendor_dir as web_vendor_dir, web_dir

    print("=" * 60)
    print("GPS Tracer 진단")
    print("=" * 60)
    print(f"실행 형태        : {'exe (frozen)' if is_frozen() else '소스'}")
    print(f"sys.executable   : {sys.executable}")
    print(f"리소스 루트      : {resource_root()}")
    print()

    def show(label, path, is_dir=False):
        exists = os.path.isdir(path) if is_dir else os.path.isfile(path)
        mark = "OK  " if exists else "없음"
        size = ""
        if exists and not is_dir:
            size = f"  ({os.path.getsize(path) / (1024 * 1024):.1f} MB)"
        print(f"  [{mark}] {label}{size}")
        print(f"         {path}")
        return exists

    print("분석 엔진")
    show("integration_blackbox.py", os.path.join(vendor_dir(), "integration_blackbox.py"))
    show("integration_avi.py", os.path.join(vendor_dir(), "integration_avi.py"))
    show("integration_mp4.py", os.path.join(vendor_dir(), "integration_mp4.py"))
    print()

    print("지도")
    show("map.html", os.path.join(web_dir(), "map.html"))
    show("maplibre-gl.js", os.path.join(web_vendor_dir(), "maplibre-gl.js"))
    extracted = extract_zipped_basemap()
    if extracted:
        print(f"  [해제] zip에서 지도를 풀었습니다: {os.path.basename(extracted)}")
    bm = basemap_path()
    expected = os.path.join(resource_root(), "assets", "(*.pmtiles 없음)")
    if bm:
        show(f"배경지도 {os.path.basename(bm)}", bm)
        print("\n  -> 배경지도가 인식됩니다. 지도에 도로/건물이 표시됩니다.")
    else:
        show("배경지도 (assets 안 .pmtiles 파일)", expected)
        print("\n  -> 배경지도가 없어 궤적만 표시됩니다(정상 동작).")
        print("     이 파일은 371MB라 GitHub(파일당 100MB 제한)에 올릴 수 없어")
        print("     .gitignore로 제외돼 있습니다. git pull로는 절대 받아지지 않으니")
        print("     USB/클라우드로 위 경로에 직접 넣고 다시 빌드하세요.")
        print("     만드는 방법은 assets/README.md 참고.")
    print()

    print("온라인 지도 (카카오맵)")
    show("map_kakao.html", os.path.join(web_dir(), "map_kakao.html"))
    show("키 파일 online_keys.json", keys_file_path())
    keys = online_keys()
    print(f"  JavaScript 키  : {mask_key(keys['js']) or '(없음 - 온라인 지도 불가)'}")
    print(f"  REST API 키    : {mask_key(keys['rest']) or '(없음 - 주소 표시 불가)'}")
    print(f"  지도 사용 방식 : {get_map_mode() or '(아직 선택 안 함 - 첫 실행 때 물어봄)'}")
    print("  카카오 디벨로퍼스 Web 플랫폼에 등록할 사이트 도메인:")
    for port in MAP_SERVER_PREFERRED_PORTS:
        print(f"    http://127.0.0.1:{port}")
    print("=" * 60)
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--diagnose":
        return _diagnose()

    if len(sys.argv) >= 2 and sys.argv[1] == "--run-engine":
        if len(sys.argv) < 4:
            print(
                'usage: app.py --run-engine <engine_name> <engine args...>\n'
                '  예: app.py --run-engine blackbox -o "출력폴더" "영상.mp4"',
                file=sys.stderr,
            )
            return 2
        import engine_entry

        return engine_entry.run(sys.argv[2], sys.argv[3:])

    # 지도 zip 해제는 Qt를 불러오기 전에 끝낸다. Qt import/초기화가 막히는 환경이
    # 있는데, 그 뒤에 두면 "지도를 넣었는데 안 나온다"로 조용히 넘어간다.
    from core.basemap import extract_zipped_basemap

    extract_zipped_basemap()

    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.styles import APP_QSS

    app = QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
