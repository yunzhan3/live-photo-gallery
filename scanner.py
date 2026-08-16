"""扫描照片文件夹：递归找出照片/视频，同名配对实况照片，按拍摄日期分组。"""
import os
from datetime import datetime

PHOTO_EXTS = {".heic", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mov", ".mp4"}

# 拍照时间读取（HEIC 需要先注册 pillow_heif）
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass
from PIL import Image


def get_taken_date(path):
    """按优先级读拍摄时间：EXIF子目录的原始拍摄时间 → 文件修改时间。返回 datetime。"""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            candidates = []
            for tag in (36867, 36868):  # DateTimeOriginal, DateTimeDigitized（多在EXIF子目录里）
                candidates.append(exif.get(tag))
                try:
                    candidates.append(exif.get_ifd(0x8769).get(tag))
                except Exception:
                    pass
            candidates.append(exif.get(306))  # IFD0 的 DateTime
            for dt in candidates:
                if dt:
                    try:
                        return datetime.strptime(dt.strip(), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        continue
    except Exception:
        pass
    return datetime.fromtimestamp(os.path.getmtime(path))


def scan(photo_dir, skip_dirs=None, cache_path=None):
    """递归扫描，返回照片条目列表。

    每个条目: {
        "photo": 相对路径(照片),
        "video": 相对路径(配对的视频, 没有则为 None),
        "album": 顶层子文件夹名(根目录则为 "未分类"),
        "date": datetime,
        "is_live": bool,
    }
    按日期从新到旧排序。
    cache_path 给一个 json 路径可以缓存拍摄日期，重复扫描时只处理新照片。
    """
    import json
    skip_dirs = {d.lower() for d in (skip_dirs or [])}
    photos = {}   # (目录, 小写主名) -> 相对路径
    videos = {}

    for root, dirs, files in os.walk(photo_dir):
        dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
        for fn in files:
            if fn.startswith("._") or fn.startswith("."):  # 跳过苹果残留垃圾文件
                continue
            stem, ext = os.path.splitext(fn)
            ext = ext.lower()
            rel = os.path.relpath(os.path.join(root, fn), photo_dir)
            key = (os.path.dirname(rel).lower(), stem.lower())
            if ext in PHOTO_EXTS:
                photos[key] = rel
            elif ext in VIDEO_EXTS:
                videos[key] = rel

    # 日期缓存：文件没变（大小+修改时间一致）就直接用上次的结果
    date_cache = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                date_cache = json.load(f)
        except Exception:
            date_cache = {}
    new_cache = {}

    def cached_date(rel):
        full = os.path.join(photo_dir, rel)
        st = os.stat(full)
        sig = [st.st_size, int(st.st_mtime)]
        hit = date_cache.get(rel)
        if hit and hit[0] == sig:
            new_cache[rel] = hit
            return datetime.fromisoformat(hit[1])
        dt = get_taken_date(full)
        new_cache[rel] = [sig, dt.isoformat()]
        return dt

    items = []
    for key, photo_rel in photos.items():
        video_rel = videos.get(key)
        parts = photo_rel.split(os.sep)
        album = parts[0] if len(parts) > 1 else "未分类"
        items.append({
            "kind": "photo",
            "photo": photo_rel,
            "video": video_rel,
            "album": album,
            "date": cached_date(photo_rel),
            "is_live": video_rel is not None,
        })
    # 没配上照片的独立视频也收进来（录屏、普通小视频）
    for key, video_rel in videos.items():
        if key in photos:
            continue
        parts = video_rel.split(os.sep)
        album = parts[0] if len(parts) > 1 else "未分类"
        items.append({
            "kind": "video",
            "photo": None,
            "video": video_rel,
            "album": album,
            "date": datetime.fromtimestamp(
                os.path.getmtime(os.path.join(photo_dir, video_rel))),
            "is_live": False,
        })
    items.sort(key=lambda x: x["date"], reverse=True)

    if cache_path:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(new_cache, f, ensure_ascii=False)
        except Exception:
            pass
    return items


def group_by_date(items):
    """按 年-月-日 分组，保持倒序。返回 [(日期字符串, [条目...]), ...]"""
    groups = {}
    order = []
    for it in items:
        label = it["date"].strftime("%Y-%m-%d")
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(it)
    return [(label, groups[label]) for label in order]


def album_summary(items):
    """统计每个相册的内容数和封面（最新一张照片）。返回 [(相册名, 数量, 封面相对路径)]"""
    summary = {}
    for it in items:  # items 已按日期倒序，第一张即最新
        if it["album"] not in summary:
            summary[it["album"]] = {"count": 0, "cover": None}
        summary[it["album"]]["count"] += 1
        if summary[it["album"]]["cover"] is None and it["photo"]:
            summary[it["album"]]["cover"] = it["photo"]
    return [(name, v["count"], v["cover"]) for name, v in summary.items()]
