"""
iPad Editor Bridge — Blenderアドオン
=====================================

iPad Safari 用ローポリ頂点融解エディタを LAN 経由で Blender と繋ぐアドオン。

- Blender 側で HTTP サーバーを起動 (デフォルト 0.0.0.0:8765)
- 同じLAN内の iPad から `http://<PCのLAN IP>:8765/` を Safari で開くとエディタが表示される
- 「Blender→」ボタン: Blender の選択メッシュを OBJ として取得
- 「→Blender」ボタン: iPad で編集したメッシュを Blender の選択オブジェクトに反映
                     (選択メッシュがなければ新規作成)

エディタ HTML はこのアドオンフォルダ内の `editor.html` を配信する。
"""

bl_info = {
    "name": "iPad Editor Bridge",
    "author": "text-editing-page",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > iPad Bridge",
    "description": "Serves the iPad low-poly vertex-dissolve editor over LAN and syncs meshes both ways",
    "category": "Import-Export",
}

import bpy
import bmesh
import threading
import socket
import queue
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
EDITOR_HTML_PATH = os.path.join(ADDON_DIR, "editor.html")


# =====================================================
# Main-thread task queue
#   HTTPリクエストはワーカースレッドで受けるが、
#   bpy 操作は必ずメインスレッドで行う必要がある。
# =====================================================
_task_queue = queue.Queue()


def _process_queue():
    """bpy.app.timers 経由でメインスレッドから定期的に呼ばれる。"""
    while True:
        try:
            task = _task_queue.get_nowait()
        except queue.Empty:
            break
        try:
            task["result"] = task["fn"]()
        except Exception as e:
            task["error"] = e
            task["traceback"] = traceback.format_exc()
        finally:
            task["event"].set()
    return 0.05  # 50ms間隔で継続


def run_on_main(fn, timeout=15.0):
    """fn をメインスレッドで実行し、結果 or 例外を返す。"""
    event = threading.Event()
    task = {"fn": fn, "result": None, "error": None, "event": event}
    _task_queue.put(task)
    if not event.wait(timeout=timeout):
        raise TimeoutError("Blender main thread did not respond in time")
    if task["error"] is not None:
        raise task["error"]
    return task["result"]


# =====================================================
# Blenderメッシュ ↔ OBJテキスト
# =====================================================
def _select_target_mesh():
    """処理対象のメッシュオブジェクトを返す (アクティブ優先)。"""
    obj = bpy.context.view_layer.objects.active
    if obj is not None and obj.type == 'MESH':
        return obj
    for o in bpy.context.selected_objects:
        if o.type == 'MESH':
            return o
    return None


def _list_candidate_meshes():
    """
    現在選択中のメッシュ一覧 (名前・頂点数・面数・アクティブか)。
    選択が0のときはアクティブメッシュ1つだけを返す (完全に何もなければ空)。
    """
    active = bpy.context.view_layer.objects.active
    active_name = active.name if active is not None else None
    seen = set()
    result = []
    for obj in bpy.context.selected_objects:
        if obj.type != 'MESH':
            continue
        if obj.name in seen:
            continue
        seen.add(obj.name)
        result.append({
            "name": obj.name,
            "vertices": len(obj.data.vertices),
            "faces": len(obj.data.polygons),
            "active": obj.name == active_name
        })
    if not result and active is not None and active.type == 'MESH':
        result.append({
            "name": active.name,
            "vertices": len(active.data.vertices),
            "faces": len(active.data.polygons),
            "active": True
        })
    return result


def _mesh_to_obj_text(name=None):
    """
    name 指定時: その名前のメッシュを送る (存在しなければ None)。
    未指定時 : アクティブ (なければ選択のうち先頭) を送る。
    """
    if name:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'MESH':
            return None
    else:
        obj = _select_target_mesh()
        if obj is None:
            return None
    mesh = obj.data
    matrix = obj.matrix_world
    lines = [f"# from Blender iPad Editor Bridge: {obj.name}"]
    for v in mesh.vertices:
        co = matrix @ v.co
        lines.append(f"v {co.x:.6f} {co.y:.6f} {co.z:.6f}")
    for poly in mesh.polygons:
        idx = " ".join(str(i + 1) for i in poly.vertices)
        lines.append(f"f {idx}")
    return "\n".join(lines) + "\n"


def _obj_text_to_mesh(text):
    """
    OBJテキストをパースし、アクティブメッシュに差し替え。
    アクティブなメッシュがなければ新規オブジェクト作成。
    """
    vertices = []
    faces = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        tag = parts[0]
        if tag == "v" and len(parts) >= 4:
            vertices.append((
                float(parts[1]),
                float(parts[2]),
                float(parts[3])
            ))
        elif tag == "f" and len(parts) >= 4:
            idx = []
            for p in parts[1:]:
                head = p.split("/")[0]
                if not head:
                    continue
                i = int(head)
                if i > 0:
                    idx.append(i - 1)
                elif i < 0:
                    idx.append(len(vertices) + i)
            if len(idx) >= 3:
                faces.append(idx)

    if not vertices:
        raise ValueError("no vertices in incoming OBJ")

    obj = _select_target_mesh()
    if obj is None:
        mesh = bpy.data.meshes.new("iPadMesh")
        obj = bpy.data.objects.new("iPadMesh", mesh)
        bpy.context.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
    else:
        mesh = obj.data

    # 既存メッシュにワールド変換が入っている場合、
    # OBJ側もワールド座標系のはずなので、
    # オブジェクト空間に戻すため matrix_world^-1 を掛ける
    matrix_inv = obj.matrix_world.inverted_safe()

    from mathutils import Vector
    bm = bmesh.new()
    bm_verts = []
    for v in vertices:
        local = matrix_inv @ Vector(v)
        bm_verts.append(bm.verts.new(local))
    bm.verts.ensure_lookup_table()
    added_faces = 0
    for f in faces:
        try:
            face_verts = [bm_verts[i] for i in f if 0 <= i < len(bm_verts)]
            if len(face_verts) < 3:
                continue
            bm.faces.new(face_verts)
            added_faces += 1
        except ValueError:
            # 既に同じ面がある → 無視
            pass
    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()
    return len(vertices), added_faces


# =====================================================
# HTTP サーバー
# =====================================================
_CORS_HEADERS = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "Content-Type"),
    ("Cache-Control", "no-store"),
]


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "iPadEditorBridge/1.0"

    def _send(self, code, body, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in _CORS_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        from urllib.parse import urlsplit, parse_qs
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        try:
            if path in ("/", "/index.html", "/editor.html"):
                self._send_editor()
            elif path == "/bridge/status":
                self._send(
                    200,
                    json.dumps({
                        "app": "ipad-editor-bridge",
                        "version": "1.1"
                    }),
                    "application/json"
                )
            elif path == "/bridge/list":
                data = run_on_main(_list_candidate_meshes)
                self._send(200, json.dumps(data), "application/json")
            elif path == "/bridge/current":
                name = query.get("name", [None])[0]
                text = run_on_main(lambda: _mesh_to_obj_text(name))
                if text is None:
                    self._send(404, "対象メッシュが見つかりません")
                else:
                    self._send(200, text)
            else:
                self._send(404, "not found")
        except Exception as e:
            self._send(500, f"error: {e}")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            if path == "/bridge/apply":
                nv, nf = run_on_main(lambda: _obj_text_to_mesh(body))
                self._send(
                    200,
                    json.dumps({"vertices": nv, "faces": nf}),
                    "application/json"
                )
            else:
                self._send(404, "not found")
        except Exception as e:
            self._send(500, f"error: {e}")

    def _send_editor(self):
        try:
            with open(EDITOR_HTML_PATH, "rb") as f:
                data = f.read()
            self._send(200, data, "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(
                500,
                "editor.html が見つかりません。アドオンフォルダに配置してください。"
            )

    def log_message(self, format, *args):
        # Blenderのコンソールを汚さない
        pass


_server = None
_server_thread = None
_server_port = None


def _start_server(port):
    global _server, _server_thread, _server_port
    if _server is not None:
        return
    _server = ThreadingHTTPServer(("0.0.0.0", port), BridgeHandler)
    _server_thread = threading.Thread(
        target=_server.serve_forever,
        name="iPadBridgeServer",
        daemon=True
    )
    _server_thread.start()
    _server_port = port
    if not bpy.app.timers.is_registered(_process_queue):
        bpy.app.timers.register(_process_queue, persistent=True)


def _stop_server():
    global _server, _server_thread, _server_port
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            pass
        _server = None
        _server_thread = None
        _server_port = None
    if bpy.app.timers.is_registered(_process_queue):
        try:
            bpy.app.timers.unregister(_process_queue)
        except ValueError:
            pass


def _get_lan_ip():
    """LAN内から見えるIPを推定 (外向きソケットでルーティングを引く)。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


# =====================================================
# Blender オペレータ & パネル
# =====================================================
class IPAD_OT_start(bpy.types.Operator):
    bl_idname = "ipad_bridge.start"
    bl_label = "サーバー開始"

    def execute(self, context):
        port = context.scene.ipad_bridge_port
        try:
            _start_server(port)
            self.report({'INFO'}, f"iPad Bridge started on :{port}")
        except OSError as e:
            self.report({'ERROR'}, f"ポート起動失敗: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


class IPAD_OT_stop(bpy.types.Operator):
    bl_idname = "ipad_bridge.stop"
    bl_label = "サーバー停止"

    def execute(self, context):
        _stop_server()
        self.report({'INFO'}, "iPad Bridge stopped")
        return {'FINISHED'}


class IPAD_PT_panel(bpy.types.Panel):
    bl_label = "iPad Bridge"
    bl_idname = "IPAD_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "iPad Bridge"

    def draw(self, context):
        layout = self.layout
        layout.prop(context.scene, "ipad_bridge_port")
        running = _server is not None
        row = layout.row()
        if running:
            row.operator("ipad_bridge.stop", icon='PAUSE')
            ip = _get_lan_ip()
            box = layout.box()
            box.label(text="iPad Safariで開く:")
            box.label(text=f"http://{ip}:{_server_port}/")
            box.separator()
            box.label(text="使い方:", icon='INFO')
            box.label(text="1. iPadで上記URLを開く")
            box.label(text="2. Blenderでメッシュを選択")
            box.label(text="3. iPadで「Blender→」で読込")
            box.label(text="4. 編集後「→Blender」で反映")
        else:
            row.operator("ipad_bridge.start", icon='PLAY')


# =====================================================
# register / unregister
# =====================================================
_classes = (IPAD_OT_start, IPAD_OT_stop, IPAD_PT_panel)


def register():
    bpy.types.Scene.ipad_bridge_port = bpy.props.IntProperty(
        name="ポート",
        default=8765,
        min=1024,
        max=65535,
        description="HTTPサーバーが待ち受けるポート番号"
    )
    for c in _classes:
        bpy.utils.register_class(c)


def unregister():
    _stop_server()
    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    try:
        del bpy.types.Scene.ipad_bridge_port
    except Exception:
        pass


if __name__ == "__main__":
    register()
