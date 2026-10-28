#!/usr/bin/env python
"""CLI: export a generated game to a browser-playable Web (HTML5) bundle.

Usage:
    conda run -n robot python scripts/export_game_web.py <game_id>
    conda run -n robot python scripts/export_game_web.py <game_id> --godot /path/to/godot
    conda run -n robot python scripts/export_game_web.py <game_id> --serve 8800

Requires a Godot 4.x binary with the matching Web export templates installed
(``--godot`` flag, ``settings.godot_path``, or ``godot`` on PATH).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from robot.games.web_export import export_game_web, publish_build  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Export a game to a Web/HTML5 bundle.")
    ap.add_argument("game_id", help="e.g. ai_1778486754_0fca33")
    ap.add_argument("--godot", default=None, help="path to the godot binary")
    ap.add_argument("--workspace", default=None, help="override the build workspace dir")
    ap.add_argument("--no-publish", action="store_true",
                    help="skip publishing to web_builds/ (only build the workspace)")
    ap.add_argument("--serve", type=int, default=None, metavar="PORT",
                    help="after export, serve the bundle on 127.0.0.1:PORT")
    args = ap.parse_args()

    build_dir = export_game_web(
        args.game_id, godot_path=args.godot, workspace=args.workspace
    )
    print(f"bundle: {build_dir}")

    if not args.no_publish:
        published = publish_build(args.game_id, build_dir)
        print(f"published: {published}")
        print(f"play url path: /play/{args.game_id}/")

    if args.serve:
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(build_dir)
        )
        with socketserver.TCPServer(("127.0.0.1", args.serve), handler) as httpd:
            print(f"serving http://127.0.0.1:{args.serve}/  (Ctrl-C to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
