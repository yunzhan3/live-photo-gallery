"""本地相册 - Flask 主程序

用法：
    1. 修改下方 PHOTO_DIR 为你的照片文件夹（移动硬盘路径）
    2. python app.py
    3. 浏览器打开 http://127.0.0.1:8000

原片永远不会被修改；所有转码结果都存进 CACHE_DIR 缓存文件夹。
"""
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile

from flask import Flask, abort, render_template, request, send_file, url_for

from scanner import scan, group_by_date, album_summary, get_taken_date, PHOTO_EXTS, VIDEO_EXTS

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass
from PIL import Image

# ============ 配置区（只需要改这里） ============
# 照片文件夹：优先用程序旁边「照片」文件夹（随硬盘走的绿色版结构），
# 找不到就在程序目录下建个「照片」文件夹放照片（或直接把这里改成你的照片路径）
_here = os.path.dirname(os.path.abspath(__file__))
_sibling = os.path.join(os.path.dirname(os.path.dirname(_here)), "照片")
PHOTO_DIR = _sibling if os.path.isdir(_sibling) else os.path.join(_here, "照片")
CACHE_DIR = os.path.join(_here, "缓存")
THUMB_SIZE = 400                # 缩略图边长（正方形裁切）
VIEW_SIZE = 2000                # 大图最长边
# ====================================================

# 支持 --port / --host 参数覆盖默认值
HOST, PORT = "127.0.0.1", 8000
import sys
for i, a in enumerate(sys.argv):
    if a == "--port" and i + 1 < len(sys.argv):
        PORT = int(sys.argv[i + 1])
    if a == "--host" and i + 1 < len(sys.argv):
        HOST = sys.argv[i + 1]

app = Flask(__name__)
os.makedirs(CACHE_DIR, exist_ok=True)

IMPORT_DIR = os.path.join(PHOTO_DIR, "待导入")   # 压缩包扔这里，网页一键导入
TRASH_DIR = os.path.join(PHOTO_DIR, "回收站")    # 删除的照片挪这里，手动清空
SKIP_DIRS = [os.path.basename(CACHE_DIR), "缓存", "待导入", "回收站"]
SCAN_CACHE = os.path.join(CACHE_DIR, "扫描缓存.json")


def safe_path(rel):
    """把相对路径还原成绝对路径，防止越权访问照片目录之外的文件。"""
    full = os.path.normpath(os.path.join(PHOTO_DIR, rel))
    if not full.startswith(os.path.normpath(PHOTO_DIR) + os.sep):
        abort(404)
    if not os.path.isfile(full):
        abort(404)
    return full


def cached_jpeg(rel, size, square):
    """把任意照片转成 JPEG 缓存（HEIC 转码 + 缩略图裁切都在这里）。"""
    src = safe_path(rel)
    key = hashlib.md5(f"{rel}|{size}|{square}|{os.path.getmtime(src)}".encode()).hexdigest()
    cache_path = os.path.join(CACHE_DIR, key + ".jpg")
    if os.path.isfile(cache_path):
        return cache_path
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if square:  # 中心裁正方形，缩略图用
                w, h = im.size
                side = min(w, h)
                im = im.crop(((w - side) // 2, (h - side) // 2,
                              (w + side) // 2, (h + side) // 2))
            im.thumbnail((size, size), Image.LANCZOS)
            im.save(cache_path, "JPEG", quality=88)
    except Exception:
        if os.path.isfile(cache_path):  # 删掉可能写了一半的缓存
            try:
                os.remove(cache_path)
            except OSError:
                pass
        abort(404)
    return cache_path


# ---------------- 页面 ----------------

@app.route("/")
def albums():
    items = scan(PHOTO_DIR, SKIP_DIRS, SCAN_CACHE)
    covers = load_covers()
    walls = []
    for name, count, cover in album_summary(items):
        walls.append({
            "name": name,
            "count": count,
            "cover": custom_cover_url(name, covers) or url_for("thumb", rel=cover),
            "custom": name in covers,
            "url": url_for("album", name=name),
        })
    return render_template("albums.html", walls=walls, total=len(items))


@app.route("/album/<path:name>")
def album(name):
    items = [it for it in scan(PHOTO_DIR, SKIP_DIRS, SCAN_CACHE) if it["album"] == name]
    groups = decorate(group_by_date(items))
    return render_template("gallery.html", title=name, groups=groups,
                           flat=build_flat(groups), album_name=name)


@app.route("/all")
def all_photos():
    items = scan(PHOTO_DIR, SKIP_DIRS, SCAN_CACHE)
    groups = decorate(group_by_date(items))
    return render_template("gallery.html", title="全部照片", groups=groups,
                           flat=build_flat(groups), album_name=None)


# ---------------- 相册封面 ----------------

COVER_DIR = os.path.join(CACHE_DIR, "封面")        # 上传的封面图存这里，不进照片目录
COVERS_FILE = os.path.join(CACHE_DIR, "封面.json")  # 相册名 → 封面设置


def load_covers():
    try:
        with open(COVERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_covers(covers):
    with open(COVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(covers, f, ensure_ascii=False, indent=1)


def custom_cover_url(name, covers=None):
    """这个相册有自定义封面就返回它的地址，没有返回 None（用默认第一张）。"""
    covers = covers if covers is not None else load_covers()
    c = covers.get(name)
    if not c:
        return None
    if c.get("kind") == "pick":
        try:
            safe_path(c["rel"])  # 文件还在才有效
            rel = c["rel"]
            if os.path.splitext(rel)[1].lower() in VIDEO_EXTS:
                return url_for("vthumb", rel=rel)  # 视频封面用网格里那一帧
            return url_for("thumb", rel=rel)
        except Exception:
            return None
    if c.get("kind") == "upload":
        if os.path.isfile(os.path.join(COVER_DIR, c.get("file", ""))):
            return url_for("cover_image", name=name)
    return None


@app.route("/cover_image/<path:name>")
def cover_image(name):
    """上传封面的正方形缩略图（和照片缩略图同款中心裁切）。"""
    c = load_covers().get(name)
    if not c or c.get("kind") != "upload":
        abort(404)
    src = os.path.join(COVER_DIR, c.get("file", ""))
    if not os.path.isfile(src):
        abort(404)
    key = hashlib.md5(f"cover|{src}|{os.path.getmtime(src)}".encode()).hexdigest()
    cache_path = os.path.join(CACHE_DIR, key + ".jpg")
    if not os.path.isfile(cache_path):
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                w, h = im.size
                side = min(w, h)
                im = im.crop(((w - side) // 2, (h - side) // 2,
                              (w + side) // 2, (h + side) // 2))
                im.thumbnail((480, 480), Image.LANCZOS)
                im.save(cache_path, "JPEG", quality=88)
        except Exception:
            abort(404)
    return send_file(cache_path)


@app.route("/api/album_cover/pick", methods=["POST"])
def cover_pick():
    """从相册里挑一张当封面。"""
    d = request.get_json(force=True)
    album, rel = d.get("album", ""), d.get("rel", "")
    safe_path(rel)  # 确认照片存在
    top = rel.replace("/", os.sep)
    top = top.split(os.sep)[0] if os.sep in top else "未分类"
    if top != album:
        return {"ok": False, "error": "这张照片不在该相册里"}, 400
    covers = load_covers()
    covers[album] = {"kind": "pick", "rel": rel}
    save_covers(covers)
    return {"ok": True}


@app.route("/api/album_cover/upload", methods=["POST"])
def cover_upload():
    """上传一张额外图片当封面。存程序缓存里，相册里看不到它。"""
    album = (request.form.get("album") or "").strip()
    f = request.files.get("file")
    if not album or f is None or not f.filename:
        return {"ok": False, "error": "缺少相册名或文件"}, 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp"):
        return {"ok": False, "error": "只支持图片文件"}, 400
    os.makedirs(COVER_DIR, exist_ok=True)
    fname = hashlib.md5(f"{album}|{time.time()}".encode()).hexdigest()[:12] + ext
    f.save(os.path.join(COVER_DIR, fname))
    covers = load_covers()
    old = covers.get(album, {})
    if old.get("kind") == "upload" and old.get("file") != fname:
        try:  # 顺手清掉旧的上传封面
            os.remove(os.path.join(COVER_DIR, old["file"]))
        except OSError:
            pass
    covers[album] = {"kind": "upload", "file": fname}
    save_covers(covers)
    return {"ok": True}


@app.route("/api/album_cover/reset", methods=["POST"])
def cover_reset():
    """恢复默认封面（自动取相册第一张）。"""
    d = request.get_json(force=True)
    album = d.get("album", "")
    covers = load_covers()
    old = covers.pop(album, None)
    if old and old.get("kind") == "upload":
        try:
            os.remove(os.path.join(COVER_DIR, old["file"]))
        except OSError:
            pass
    save_covers(covers)
    return {"ok": True}


def build_flat(groups):
    """拍平成 JS 用的列表：类型 + 相对路径 + 大图地址 + 视频地址。"""
    return [{"kind": it["kind"],
             "rel": it["photo"] if it["kind"] == "photo" else it["video"],
             "view": it["view"], "video": it["video_url"]}
            for _, items in groups for it in items]


def decorate(groups):
    """给模板补上缩略图/大图/视频地址。"""
    for _, items in groups:
        for it in items:
            if it["kind"] == "video":
                it["thumb"] = url_for("vthumb", rel=it["video"])
                it["view"] = None
                it["video_url"] = url_for("video", rel=it["video"])
            else:
                it["thumb"] = url_for("thumb", rel=it["photo"])
                it["view"] = url_for("view", rel=it["photo"])
                it["video_url"] = url_for("video", rel=it["video"]) if it["video"] else None
    return groups


# ---------------- 文件 ----------------

FFMPEG = os.path.join(_here, "ffmpeg.exe")
if not os.path.isfile(FFMPEG):
    FFMPEG = shutil.which("ffmpeg")  # 绿色版自带，开发环境兜底


def video_frame(rel):
    """从视频里截一帧做缩略图，和照片一样摆进网格。"""
    src = safe_path(rel)
    key = hashlib.md5(f"video|{rel}|{os.path.getmtime(src)}".encode()).hexdigest()
    cache_path = os.path.join(CACHE_DIR, key + ".jpg")
    if os.path.isfile(cache_path):
        return cache_path
    import subprocess
    try:
        subprocess.run(
            [FFMPEG, "-ss", "0.5", "-i", src, "-frames:v", "1",
             "-vf", f"scale={THUMB_SIZE}:{THUMB_SIZE}:force_original_aspect_ratio=increase,"
                    f"crop={THUMB_SIZE}:{THUMB_SIZE}",
             "-y", cache_path],
            capture_output=True, timeout=30,
            creationflags=0x08000000 if os.name == "nt" else 0)  # 不弹黑窗
    except Exception:
        pass
    if not os.path.isfile(cache_path):  # 截图失败就画个灰色占位图
        ph = Image.new("RGB", (THUMB_SIZE, THUMB_SIZE), (200, 200, 200))
        from PIL import ImageDraw
        d = ImageDraw.Draw(ph)
        c = THUMB_SIZE // 2
        d.polygon([(c - 60, c - 80), (c - 60, c + 80), (c + 90, c)], fill=(255, 255, 255))
        ph.save(cache_path, "JPEG", quality=85)
    return cache_path


@app.route("/vthumb/<path:rel>")
def vthumb(rel):
    if os.path.splitext(rel)[1].lower() not in VIDEO_EXTS:
        abort(404)
    return send_file(video_frame(rel), mimetype="image/jpeg")


@app.route("/thumb/<path:rel>")
def thumb(rel):
    return send_file(cached_jpeg(rel, THUMB_SIZE, square=True), mimetype="image/jpeg")


@app.route("/view/<path:rel>")
def view(rel):
    ext = os.path.splitext(rel)[1].lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:  # 浏览器本来就认的格式，直接发原图
        return send_file(safe_path(rel))
    return send_file(cached_jpeg(rel, VIEW_SIZE, square=False), mimetype="image/jpeg")


@app.route("/video/<path:rel>")
def video(rel):
    if os.path.splitext(rel)[1].lower() not in VIDEO_EXTS:
        abort(404)
    return send_file(safe_path(rel))


@app.route("/api/info/<path:rel>")
def api_info(rel):
    """照片详细信息：文件名、相册、拍摄时间、尺寸、大小、设备、实况视频。"""
    full = safe_path(rel)

    def human(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
            n /= 1024

    info = {
        "name": os.path.basename(rel),
        "album": rel.replace("/", os.sep).split(os.sep)[0] if os.sep in rel.replace("/", os.sep) else "未分类",
        "filesize": human(os.path.getsize(full)),
        "taken": get_taken_date(full).strftime("%Y-%m-%d %H:%M:%S"),
        "width": None, "height": None, "device": None,
        "video": None,
    }
    try:
        with Image.open(full) as im:
            info["width"], info["height"] = im.size
            exif = im.getexif()
            make = (exif.get(271) or "").strip()
            model = (exif.get(272) or "").strip()
            device = " ".join(x for x in (make, model) if x)
            if device:
                info["device"] = device
    except Exception:
        pass
    # 配对的实况视频
    base = os.path.splitext(full)[0]
    for vext in (".mov", ".MOV", ".mp4", ".MP4"):
        v = base + vext
        if os.path.isfile(v):
            info["video"] = f"{os.path.basename(v)}（{human(os.path.getsize(v))}）"
            break
    return info


# ---------------- 整理 ----------------

@app.route("/api/albums")
def api_albums():
    """现有相册名列表（顶层文件夹）。"""
    names = []
    if os.path.isdir(PHOTO_DIR):
        for entry in os.listdir(PHOTO_DIR):
            full = os.path.join(PHOTO_DIR, entry)
            if os.path.isdir(full) and entry not in SKIP_DIRS and not entry.startswith("."):
                names.append(entry)
    return {"albums": sorted(names)}


def unique_dest(dest_dir, filename):
    """目标位置已有同名文件时，自动加 (2) (3) 后缀，绝不覆盖。"""
    stem, ext = os.path.splitext(filename)
    dst = os.path.join(dest_dir, filename)
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(dest_dir, f"{stem} ({n}){ext}")
    return dst


@app.route("/move", methods=["POST"])
def move():
    """把勾选的照片（连同配对的实况视频）移动到目标相册文件夹。"""
    data = request.get_json(force=True)
    targets = data.get("targets", [])
    album = (data.get("album") or "").strip()
    if not album or any(c in album for c in '\\/:*?"<>|') or album in (".", ".."):
        return {"ok": False, "error": "相册名不合法"}, 400
    dest_dir = PHOTO_DIR if album == "未分类" else os.path.join(PHOTO_DIR, album)
    os.makedirs(dest_dir, exist_ok=True)

    moved, errors = 0, []
    for rel in targets:
        try:
            src = safe_path(rel)  # 已校验在照片目录内
            base = os.path.splitext(src)[0]
            companions = [src]
            seen = {os.path.normcase(src)}
            for vext in (".mov", ".MOV", ".mp4", ".MP4"):  # 实况视频一起搬
                v = base + vext
                if os.path.isfile(v) and os.path.normcase(v) not in seen:
                    seen.add(os.path.normcase(v))
                    companions.append(v)
            for p in companions:
                shutil.move(p, unique_dest(dest_dir, os.path.basename(p)))
            moved += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    return {"ok": True, "moved": moved, "errors": errors}


@app.route("/delete", methods=["POST"])
def delete():
    """删除勾选的照片：不真删，挪到 回收站/ 文件夹（实况视频一起挪）。"""
    data = request.get_json(force=True)
    targets = data.get("targets", [])
    os.makedirs(TRASH_DIR, exist_ok=True)

    deleted, errors = 0, []
    for rel in targets:
        try:
            src = safe_path(rel)
            base = os.path.splitext(src)[0]
            companions = [src]
            seen = {os.path.normcase(src)}
            for vext in (".mov", ".MOV", ".mp4", ".MP4"):
                v = base + vext
                if os.path.isfile(v) and os.path.normcase(v) not in seen:
                    seen.add(os.path.normcase(v))
                    companions.append(v)
            for p in companions:
                shutil.move(p, unique_dest(TRASH_DIR, os.path.basename(p)))
            deleted += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    return {"ok": True, "deleted": deleted, "errors": errors}


def normalize_export_name(filename):
    """一刻下载的双后缀名（IMG_0430.HEIC.heic）规范化成 IMG_0430.heic，兼容性更好。"""
    stem, ext = os.path.splitext(filename)
    if os.path.splitext(stem)[1].lower() in MEDIA_EXTS:
        return os.path.splitext(stem)[0] + ext.lower()
    return filename


@app.route("/export", methods=["POST"])
def export():
    """把勾选的照片（连同配对视频）复制到桌面新建的小文件夹，用于导回手机。
    实况配对身份证（UUID）自动补齐，思路参考 live-photo-box 的修复逻辑：
    照片有 UUID → 写进缺证的视频；视频有 UUID → 直接沿用；
    两边都没有（一刻把两端都剥了）→ 现场生成一个写进视频。写完逐一校验。"""
    data = request.get_json(force=True)
    targets = data.get("targets", [])
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(os.path.expanduser("~"), "Desktop", f"实况导出_{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    exported, repaired, generated, errors = 0, 0, 0, []
    for rel in targets:
        try:
            src = safe_path(rel)
            base = os.path.splitext(src)[0]
            companions = [src]
            seen = {os.path.normcase(src)}
            for vext in (".mov", ".MOV", ".mp4", ".MP4"):
                v = base + vext
                if os.path.isfile(v) and os.path.normcase(v) not in seen:
                    seen.add(os.path.normcase(v))
                    companions.append(v)
            # 配对 UUID：优先用照片里的，照片被剥了就去视频里找
            pair_uuid = None
            if len(companions) > 1:
                pair_uuid = find_pair_uuid(src) or find_video_uuid(companions[1])
            for i, p in enumerate(companions):
                dst = unique_dest(out_dir, normalize_export_name(os.path.basename(p)))
                if i == 0:
                    shutil.copy2(p, dst)  # 照片原样复制
                    continue
                if video_has_identifier(p):
                    shutil.copy2(p, dst)  # 视频自带身份证，原样复制
                    continue
                is_new = False
                if not pair_uuid:
                    pair_uuid = str(uuid.uuid4()).upper()  # 两端都没证，现场办一个
                    is_new = True
                if write_identifier(p, dst, pair_uuid) and video_has_identifier(dst):
                    if is_new:
                        generated += 1
                    else:
                        repaired += 1
                else:
                    # 补写失败别耽误导出：删掉半成品，原样复制
                    if os.path.isfile(dst):
                        os.remove(dst)
                    shutil.copy2(p, dst)
                    errors.append(f"{os.path.basename(p)}: 身份证补写失败，已原样导出")
            exported += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    return {"ok": True, "exported": exported, "repaired": repaired,
            "generated": generated, "folder": out_dir, "errors": errors}


_PAIR_MARKER = b"com.apple.quicktime.content.identifier"
_UUID_RE = None


def find_pair_uuid(photo_path):
    """从实况照片的静态文件里找配对 UUID（通常在文件头部的元数据里）。"""
    global _UUID_RE
    import re
    if _UUID_RE is None:
        _UUID_RE = re.compile(
            rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
            rb"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
    try:
        with open(photo_path, "rb") as f:
            head = f.read(3 * 1024 * 1024)
        m = _UUID_RE.search(head)
        return m.group().decode() if m else None
    except Exception:
        return None


def video_has_identifier(video_path):
    try:
        with open(video_path, "rb") as f:
            return _PAIR_MARKER in f.read()
    except Exception:
        return False


def find_video_uuid(video_path):
    """从视频里找配对 UUID：定位身份证标记，取它后面最近的 UUID。
    视频的元数据可能在头部也可能在尾部，两头都搜。"""
    global _UUID_RE
    import re
    if _UUID_RE is None:
        _UUID_RE = re.compile(
            rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
            rb"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
    try:
        size = os.path.getsize(video_path)
        with open(video_path, "rb") as f:
            head = f.read(2 * 1024 * 1024)
            tail = b""
            if size > len(head):
                f.seek(max(0, size - 2 * 1024 * 1024))
                tail = f.read()
        for blob in (head, tail):
            idx = blob.find(_PAIR_MARKER)
            if idx >= 0:
                m = _UUID_RE.search(blob, idx)
                if m:
                    return m.group().decode().upper()
        return None
    except Exception:
        return None


def write_identifier(src_mov, dst_mov, uuid_str):
    """用 ffmpeg 把配对 UUID 写进视频副本（元数据级操作，秒完成）。"""
    import subprocess
    try:
        r = subprocess.run(
            [FFMPEG, "-i", src_mov, "-c", "copy", "-movflags", "use_metadata_tags",
             "-metadata", f"com.apple.quicktime.content.identifier={uuid_str}",
             "-y", dst_mov],
            capture_output=True, timeout=60,
            creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode == 0 and os.path.isfile(dst_mov):
            # 保留原始时间
            st = os.stat(src_mov)
            os.utime(dst_mov, (st.st_atime, st.st_mtime))
            return True
    except Exception:
        pass
    return False


# ---------------- 压缩包导入 ----------------

MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS


def extract_zip_recursive(zpath, out_dir, stats):
    """拆压缩包，遇到套娃 zip 继续拆，照片/视频提到 out_dir。"""
    try:
        zf = zipfile.ZipFile(zpath, metadata_encoding="gbk")  # 兼容中文文件名
    except zipfile.BadZipFile:
        stats["errors"].append(f"{os.path.basename(zpath)}: 压缩包损坏")
        return
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = os.path.basename(info.filename)
            if not name or name.startswith("._"):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext == ".zip":
                tmp_zip = os.path.join(out_dir, "__套娃__" + name)
                with zf.open(info) as f, open(tmp_zip, "wb") as o:
                    shutil.copyfileobj(f, o)
                extract_zip_recursive(tmp_zip, out_dir, stats)
                os.remove(tmp_zip)
            elif ext in MEDIA_EXTS:
                dst = unique_dest(out_dir, name)
                with zf.open(info) as f, open(dst, "wb") as o:
                    shutil.copyfileobj(f, o)
                try:  # 保留压缩包里的原始时间
                    ts = time.mktime(info.date_time + (0, 0, -1))
                    os.utime(dst, (ts, ts))
                except Exception:
                    pass
                stats["media"] += 1


@app.route("/api/import", methods=["POST"])
def api_import():
    """把 待导入/ 里所有压缩包拆开，媒体文件导入目标相册。"""
    data = request.get_json(force=True)
    album = (data.get("album") or "未分类").strip()
    if not album or any(c in album for c in '\\/:*?"<>|') or album in (".", ".."):
        return {"ok": False, "error": "相册名不合法"}, 400
    dest_dir = PHOTO_DIR if album == "未分类" else os.path.join(PHOTO_DIR, album)
    os.makedirs(dest_dir, exist_ok=True)

    stats = {"zips": 0, "media": 0, "errors": []}
    tmp = tempfile.mkdtemp(prefix="相册导入_")
    try:
        if os.path.isdir(IMPORT_DIR):
            for fn in sorted(os.listdir(IMPORT_DIR)):
                if not fn.lower().endswith(".zip"):
                    continue
                zp = os.path.join(IMPORT_DIR, fn)
                before = stats["media"]
                extract_zip_recursive(zp, tmp, stats)
                stats["zips"] += 1
                if stats["media"] > before:  # 提出东西了才删原压缩包
                    os.remove(zp)
        for fn in os.listdir(tmp):
            if fn.startswith("__套娃__"):
                continue
            shutil.move(os.path.join(tmp, fn), unique_dest(dest_dir, fn))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, **stats}


def preheat():
    """后台慢慢把所有缩略图转码好，浏览时就不用等了。"""
    import time
    time.sleep(2)  # 等服务先起来
    try:
        items = scan(PHOTO_DIR, SKIP_DIRS, SCAN_CACHE)
        total = len(items)
        for i, it in enumerate(items, 1):
            try:
                if it["kind"] == "video":
                    video_frame(it["video"])
                else:
                    cached_jpeg(it["photo"], THUMB_SIZE, square=True)
            except Exception:
                pass
            if i % 20 == 0 or i == total:
                print(f"缩略图预热: {i}/{total}", flush=True)
        print("缩略图预热完成 ✓", flush=True)
    except Exception as e:
        print(f"预热出错: {e}", flush=True)


if __name__ == "__main__":
    import threading
    import webbrowser
    print(f"照片目录: {PHOTO_DIR}")
    print(f"相册地址: http://{HOST}:{PORT}（浏览器会自动打开）")
    threading.Thread(target=preheat, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    try:
        app.run(host=HOST, port=PORT, threaded=True)
    except OSError:
        print()
        print("=" * 50)
        print("端口被占用啦——相册可能已经在运行了。")
        print(f"直接打开浏览器访问 http://127.0.0.1:{PORT} 就行；")
        print("或者先找到另一个相册的黑窗口关掉，再双击启动。")
        print("=" * 50)
