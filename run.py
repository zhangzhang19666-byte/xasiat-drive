import os
import re
import json
import time
import shutil
import zipfile
import asyncio
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

START_ID = int(os.environ.get("XASIAT_START_ID", "1"))
ALBUMS_URL = os.environ.get("XASIAT_ALBUMS_URL", "https://www.xasiat.com/albums/")
BASE_URL = os.environ.get(
    "XASIAT_BASE_URL",
    "https://www.xasiat.com/albums/{}/cosplay-g44-32p-396mb/",
)
SITE = "https://www.xasiat.com/"

REMOTE = os.environ.get("RCLONE_REMOTE", "drive")
DEST = os.environ.get("XASIAT_DEST", "").strip("/")
DEST_URI = f"{REMOTE}:{DEST}" if DEST else f"{REMOTE}:"
STATE_URI = f"{REMOTE}:{DEST}/_state" if DEST else f"{REMOTE}:_state"

WORK = Path(os.environ.get("XASIAT_WORK", "work"))
STATE_LOCAL = WORK / "_state"

DOWNLOADED_FILE = STATE_LOCAL / "downloaded.txt"
CURSOR_FILE = STATE_LOCAL / "cursor.txt"
RETRY_FILE = STATE_LOCAL / "retry.txt"
FAILED_FILE = STATE_LOCAL / "failed.txt"
HALT_FILE = STATE_LOCAL / "HALT"
DONE_FILE = STATE_LOCAL / "DONE"

CONCURRENCY = int(os.environ.get("CONCURRENCY", "30"))
RETRIES = int(os.environ.get("RETRIES", "5"))
MAX_RETRY_ROUNDS = int(os.environ.get("MAX_RETRY_ROUNDS", "3"))
DOWNLOAD_MIN = float(os.environ.get("DOWNLOAD_MIN", "300"))
WINDOW_MIN = float(os.environ.get("WINDOW_MIN", "50"))
MAX_CHAIN = int(os.environ.get("MAX_CHAIN", "3"))
CHAIN = int(os.environ.get("CHAIN", "0"))
MODE = os.environ.get("MODE", "run").strip().lower()
BACKEND = os.environ.get("HTTP_BACKEND", "curl_cffi").strip().lower()
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")
CLEAR_HALT = os.environ.get("CLEAR_HALT", "0").strip().lower() in ("1", "true", "yes")

GH_TOKEN = os.environ.get("GH_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REF = os.environ.get("GITHUB_REF_NAME", "main")
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "xasiat.yml")

PAGE_TIMEOUT = float(os.environ.get("PAGE_TIMEOUT", "60"))
IMG_TIMEOUT = float(os.environ.get("IMG_TIMEOUT", "180"))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
IMG_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"


def _headers(backend, extra=None):
    h = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if backend == "aiohttp":
        h["User-Agent"] = USER_AGENT
        h["Connection"] = "keep-alive"
    if extra:
        h.update(extra)
    return h


def make_session(backend):
    if backend == "aiohttp":
        import aiohttp

        conn = aiohttp.TCPConnector(
            limit=CONCURRENCY, limit_per_host=CONCURRENCY, ssl=False
        )
        return aiohttp.ClientSession(connector=conn, headers=_headers("aiohttp"))
    from curl_cffi.requests import AsyncSession

    return AsyncSession(impersonate=IMPERSONATE, headers=_headers("curl_cffi"))


async def http_get(session, url, headers, sec, backend):
    if backend == "aiohttp":
        import aiohttp

        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=sec)
        ) as resp:
            return resp.status, await resp.read()
    resp = await session.get(url, headers=headers, timeout=sec)
    return resp.status_code, resp.content


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip().rstrip(" .")
    return name


def get_filename_from_original(data_original):
    if not data_original:
        return None
    clean_url = data_original.split("?", 1)[0].rstrip("/")
    filename = os.path.basename(urlparse(clean_url).path)
    if not filename:
        return None
    if not re.search(r"\.(jpg|jpeg|png|webp|gif)$", filename, re.IGNORECASE):
        return None
    return filename


def is_image_url(url):
    return bool(
        re.search(r"\.(jpg|jpeg|png|webp|gif)(?:[/ ?]|$)", url, re.IGNORECASE)
    )


def parse_page(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        return None, []
    folder_name = safe_filename(h1.get_text(" ", strip=True))
    container = soup.select_one(".images")
    if not container:
        return folder_name, []
    image_list = []
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        if not is_image_url(href):
            continue
        img = a.find("img")
        if not img:
            continue
        data_original = img.get("data-original", "").strip()
        if not data_original:
            continue
        filename = get_filename_from_original(data_original)
        if not filename:
            continue
        image_url = urljoin(page_url, href)
        if any(x["filename"] == filename for x in image_list):
            continue
        image_list.append({"url": image_url, "filename": filename})
    return folder_name, image_list


def _challenge(txt):
    t = txt[:8000].lower()
    return (
        "just a moment" in t
        or "cf-challenge" in t
        or "attention required" in t
        or "enable javascript and cookies" in t
    )


async def fetch_page(session, album_id, backend):
    page_url = BASE_URL.format(album_id)
    for attempt in range(1, RETRIES + 1):
        try:
            status, body = await http_get(
                session,
                page_url,
                _headers(backend, {"Referer": SITE}),
                PAGE_TIMEOUT,
                backend,
            )
            if status == 404:
                return {"status": "404", "folder": None, "images": []}
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            html = body.decode("utf-8", "ignore")
            folder, images = parse_page(html, page_url)
            if not folder:
                return {"status": "no_h1", "folder": None, "images": []}
            if not images:
                return {"status": "empty", "folder": folder, "images": []}
            return {"status": "ok", "folder": folder, "images": images}
        except Exception as e:
            print(f"[{album_id}] 获取页面失败 ({attempt}/{RETRIES})：{e}", flush=True)
            if attempt < RETRIES:
                await asyncio.sleep(1.5 * attempt)
    return {"status": "error", "folder": None, "images": []}


async def get_live_max(session, backend):
    try:
        status, body = await http_get(
            session, ALBUMS_URL, _headers(backend, {"Referer": SITE}), PAGE_TIMEOUT, backend
        )
        if status != 200:
            print(f"albums 页 HTTP {status}", flush=True)
            return None
        txt = body.decode("utf-8", "ignore")
        nums = [int(x) for x in re.findall(r"https://www\.xasiat\.com/albums/(\d+)/", txt)]
        return max(nums) if nums else None
    except Exception as e:
        print(f"获取实时最大 ID 失败：{e}", flush=True)
        return None


async def download_image(session, sem, album_id, image_url, filepath, index, total, backend):
    if filepath.exists() and filepath.stat().st_size > 0:
        return "skip"
    async with sem:
        for attempt in range(1, RETRIES + 1):
            temp_file = filepath.with_name(filepath.name + ".part")
            try:
                headers = _headers(
                    backend,
                    {
                        "Referer": BASE_URL.format(album_id),
                        "Accept": IMG_ACCEPT,
                    },
                )
                status, data = await http_get(
                    session, image_url, headers, IMG_TIMEOUT, backend
                )
                if status != 200:
                    raise RuntimeError(f"HTTP {status}")
                if not data:
                    raise RuntimeError("下载文件为空")
                temp_file.write_bytes(data)
                os.replace(temp_file, filepath)
                print(f"    [{index}/{total}] ✓ {filepath.name}", flush=True)
                return "success"
            except Exception as e:
                if temp_file.exists():
                    try:
                        temp_file.unlink()
                    except Exception:
                        pass
                if attempt < RETRIES:
                    await asyncio.sleep(1.5 * attempt)
                else:
                    print(f"    [{index}/{total}] ✗ {filepath.name} 失败：{e}", flush=True)
    return "failed"


async def download_album_images(session, album_id, folder, images, backend):
    save_dir = WORK / folder
    save_dir.mkdir(parents=True, exist_ok=True)
    total = len(images)
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        download_image(
            session, sem, album_id, item["url"], save_dir / item["filename"], i, total, backend
        )
        for i, item in enumerate(images, start=1)
    ]
    results = await asyncio.gather(*tasks)
    return results.count("success"), results.count("skip"), results.count("failed")


def make_zip(folder, save_dir):
    zip_path = WORK / (folder + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for p in sorted(save_dir.rglob("*")):
            if p.is_file():
                z.write(p, arcname=str(Path(folder) / p.relative_to(save_dir)))
    return zip_path


def rclone(args):
    try:
        p = subprocess.run(["rclone", *args], capture_output=True, text=True)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except FileNotFoundError:
        return 127, "", "rclone not found"
    except Exception as e:
        return 1, "", str(e)


def pull_state():
    STATE_LOCAL.mkdir(parents=True, exist_ok=True)
    rclone(["copy", STATE_URI, str(STATE_LOCAL)])


def push_state():
    rc, out, err = rclone(["copy", str(STATE_LOCAL), STATE_URI])
    if rc != 0:
        print(f"状态回写失败：{err.strip()[:200]}", flush=True)
    return rc


def drive_free_bytes():
    rc, out, err = rclone(["about", f"{REMOTE}:", "--json"])
    if rc != 0:
        return None
    try:
        return int(json.loads(out).get("free"))
    except Exception:
        return None


def halt(reason):
    HALT_FILE.write_text(reason, encoding="utf-8")
    push_state()
    print(f"[HALT] {reason}", flush=True)


def upload_zip(zip_path):
    size = zip_path.stat().st_size
    free = drive_free_bytes()
    if free is not None and free < size + 100 * 1024 * 1024:
        halt(f"Drive 剩余 {free} < 需要 {size}")
        return False
    rc, out, err = rclone(["copy", str(zip_path), DEST_URI, "--stats-one-line"])
    if rc != 0:
        text = (err + out).lower()
        print(f"上传失败：{err.strip()[:300]}", flush=True)
        if "quota" in text or "storagequota" in text or "insufficient" in text:
            halt("Drive 容量不足/配额超限，无法上传")
        return False
    return True


def append_downloaded(album_id, folder):
    with open(DOWNLOADED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{album_id}|{folder}\n")


def set_cursor(val):
    CURSOR_FILE.write_text(str(val), encoding="utf-8")


def load_cursor():
    if CURSOR_FILE.exists():
        try:
            return int(CURSOR_FILE.read_text().strip())
        except Exception:
            return None
    return None


def load_retry():
    d = {}
    if RETRY_FILE.exists():
        for line in RETRY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if parts[0].isdigit():
                d[int(parts[0])] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    return d


def save_retry(d):
    RETRY_FILE.write_text("".join(f"{k}|{v}\n" for k, v in sorted(d.items())), encoding="utf-8")


def record_failed(album_id, reason):
    with open(FAILED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{album_id}|{reason}\n")


async def process_album(session, album_id, backend, retry):
    result = await fetch_page(session, album_id, backend)
    status = result["status"]

    if status in ("404", "no_h1"):
        print(f"[{album_id}] 跳过（{status}）", flush=True)
        retry.pop(album_id, None)
        return True

    if status == "empty":
        print(f"[{album_id}] 空相册：{result['folder']}", flush=True)
        append_downloaded(album_id, result["folder"])
        retry.pop(album_id, None)
        return True

    if status == "error":
        cnt = retry.get(album_id, 0) + 1
        if cnt >= MAX_RETRY_ROUNDS:
            record_failed(album_id, "page_error")
            retry.pop(album_id, None)
            print(f"[{album_id}] 页面反复失败，记入 failed", flush=True)
            return True
        retry[album_id] = cnt
        print(f"[{album_id}] 页面失败，留待重试（第 {cnt} 次）", flush=True)
        return False

    folder = result["folder"]
    images = result["images"]
    print(f"[{album_id}] 文件夹：{folder}  图片数：{len(images)}", flush=True)
    ok, skip, fail = await download_album_images(session, album_id, folder, images, backend)
    save_dir = WORK / folder

    if fail == 0:
        zip_path = make_zip(folder, save_dir)
        if not upload_zip(zip_path):
            try:
                zip_path.unlink()
            except Exception:
                pass
            print(f"[{album_id}] 上传失败，保留待重试", flush=True)
            return False
        shutil.rmtree(save_dir, ignore_errors=True)
        try:
            zip_path.unlink()
        except Exception:
            pass
        append_downloaded(album_id, folder)
        retry.pop(album_id, None)
        print(f"[{album_id}] ✓ 完成 成功{ok} 跳过{skip} 失败{fail} 已上传", flush=True)
        return True

    cnt = retry.get(album_id, 0) + 1
    if cnt >= MAX_RETRY_ROUNDS:
        shutil.rmtree(save_dir, ignore_errors=True)
        record_failed(album_id, "img_fail")
        retry.pop(album_id, None)
        print(f"[{album_id}] 图片反复失败（失败{fail}），记入 failed", flush=True)
        return True
    retry[album_id] = cnt
    print(f"[{album_id}] 存在失败图片{fail}，不上传，留待重试（第 {cnt} 次）", flush=True)
    return False


def trigger_next(next_chain):
    if not (REPO and GH_TOKEN):
        print("缺少 GITHUB_REPOSITORY/GH_TOKEN，无法串联", flush=True)
        return
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    payload = {"ref": REF, "inputs": {"mode": "run", "chain": str(next_chain), "clear_halt": "false"}}
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        import curl_cffi

        r = curl_cffi.post(url, headers=headers, json=payload, timeout=30)
        print(f"触发下一个 job chain={next_chain}：HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"串联失败：{e}", flush=True)


async def run():
    WORK.mkdir(parents=True, exist_ok=True)
    STATE_LOCAL.mkdir(parents=True, exist_ok=True)

    if CLEAR_HALT:
        rclone(["delete", f"{STATE_URI}/HALT"])
        rclone(["delete", f"{STATE_URI}/DONE"])
        for f in (HALT_FILE, DONE_FILE):
            try:
                f.unlink()
            except Exception:
                pass

    pull_state()

    if HALT_FILE.exists():
        print(f"[HALT] {HALT_FILE.read_text(errors='ignore').strip()} —— 退出", flush=True)
        return
    if DONE_FILE.exists():
        print("[DONE] 已全部完成 —— 退出", flush=True)
        return

    retry = load_retry()
    cursor = load_cursor() or START_ID
    finished = False

    async with make_session(BACKEND) as session:
        live_max = await get_live_max(session, BACKEND)
        if live_max is None:
            print("无法获取实时最大 ID —— 安全退出", flush=True)
            return
        print(f"实时最大 ID={live_max} 游标={cursor} 后端={BACKEND} 链={CHAIN}", flush=True)

        if cursor > live_max and not retry:
            DONE_FILE.write_text("done", encoding="utf-8")
            push_state()
            print("已全部完成", flush=True)
            return

        deadline = time.monotonic() + DOWNLOAD_MIN * 60

        for rid in sorted(list(retry.keys())):
            if HALT_FILE.exists() or time.monotonic() >= deadline:
                break
            await process_album(session, rid, BACKEND, retry)
            save_retry(retry)
            push_state()

        while cursor <= live_max:
            if HALT_FILE.exists():
                break
            if time.monotonic() >= deadline:
                print("到达下载时间预算 —— 收尾", flush=True)
                break
            await process_album(session, cursor, BACKEND, retry)
            cursor += 1
            set_cursor(cursor)
            save_retry(retry)
            push_state()

        finished = cursor > live_max and not retry

    if HALT_FILE.exists():
        print("处于 HALT —— 不串联", flush=True)
        return
    if finished:
        DONE_FILE.write_text("done", encoding="utf-8")
        push_state()
        print("全部完成 —— 不串联", flush=True)
        return
    if CHAIN < MAX_CHAIN - 1:
        trigger_next(CHAIN + 1)
    else:
        print(f"达到当日串联上限 {MAX_CHAIN} 轮 —— 等明日 cron", flush=True)


async def probe():
    print("================ PROBE ================", flush=True)
    for backend in ("aiohttp", "curl_cffi"):
        print(f"-------------- {backend} --------------", flush=True)
        try:
            async with make_session(backend) as session:
                t0 = time.time()
                st, body = await http_get(
                    session, ALBUMS_URL, _headers(backend, {"Referer": SITE}), PAGE_TIMEOUT, backend
                )
                txt = body.decode("utf-8", "ignore")
                nums = [
                    int(x)
                    for x in re.findall(r"https://www\.xasiat\.com/albums/(\d+)/", txt)
                ]
                print(
                    f"[{backend}] albums页 status={st} bytes={len(body)} "
                    f"用时={time.time()-t0:.2f}s 挑战={_challenge(txt)} "
                    f"最大ID={max(nums) if nums else None}",
                    flush=True,
                )
                if not nums:
                    continue
                aid = max(nums)
                page_url = BASE_URL.format(aid)
                t0 = time.time()
                st2, body2 = await http_get(
                    session, page_url, _headers(backend, {"Referer": SITE}), PAGE_TIMEOUT, backend
                )
                folder, imgs = parse_page(body2.decode("utf-8", "ignore"), page_url)
                print(
                    f"[{backend}] 相册页 id={aid} status={st2} bytes={len(body2)} "
                    f"用时={time.time()-t0:.2f}s 文件夹={folder} 图片数={len(imgs)}",
                    flush=True,
                )
                if not imgs:
                    continue
                img_url = imgs[0]["url"]
                t0 = time.time()
                st3, body3 = await http_get(
                    session,
                    img_url,
                    _headers(backend, {"Referer": page_url, "Accept": IMG_ACCEPT}),
                    IMG_TIMEOUT,
                    backend,
                )
                print(
                    f"[{backend}] 图片 status={st3} bytes={len(body3)} 用时={time.time()-t0:.2f}s",
                    flush=True,
                )
        except Exception as e:
            print(f"[{backend}] 异常：{e}", flush=True)
    print("================ PROBE END ================", flush=True)


async def bench():
    import statistics

    target = int(os.environ.get("BENCH_IMAGES", "120"))
    urls = []
    async with make_session(BACKEND) as session:
        live = await get_live_max(session, BACKEND)
        print(f"BENCH live_max={live}", flush=True)
        aid = live
        while aid > 0 and len(urls) < target and aid > live - 80:
            r = await fetch_page(session, aid, BACKEND)
            if r["status"] == "ok":
                urls.extend(x["url"] for x in r["images"])
            aid -= 1
    urls = urls[:target]
    print(f"BENCH images={len(urls)} concurrent={CONCURRENCY}", flush=True)
    if not urls:
        print("BENCH no urls", flush=True)
        return

    for backend in ("aiohttp", "curl_cffi"):
        d = WORK / f"bench_{backend}"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        sem = asyncio.Semaphore(CONCURRENCY)
        lat = []

        async def one(i, u):
            async with sem:
                t = time.monotonic()
                try:
                    st, data = await http_get(
                        session,
                        u,
                        _headers(backend, {"Accept": IMG_ACCEPT}),
                        IMG_TIMEOUT,
                        backend,
                    )
                    lat.append(time.monotonic() - t)
                    if st == 200 and data:
                        (d / f"{i}.bin").write_bytes(data)
                        return len(data)
                except Exception:
                    pass
                return -1

        t0 = time.monotonic()
        async with make_session(backend) as session:
            res = await asyncio.gather(*[one(i, u) for i, u in enumerate(urls)])
        dt = time.monotonic() - t0
        ok = [x for x in res if x and x > 0]
        mb = sum(ok) / 1048576
        speed = mb / dt if dt else 0
        avglat = (statistics.mean(lat) * 1000) if lat else 0
        print(
            f"[BENCH {backend}] ok={len(ok)}/{len(urls)} bytes={sum(ok)} "
            f"time={dt:.2f}s speed={speed:.2f}MB/s avg_lat={avglat:.0f}ms",
            flush=True,
        )
        shutil.rmtree(d, ignore_errors=True)


def main():
    try:
        if MODE == "probe":
            asyncio.run(probe())
        elif MODE == "bench":
            asyncio.run(bench())
        else:
            asyncio.run(run())
    except KeyboardInterrupt:
        print("被中断", flush=True)


if __name__ == "__main__":
    main()
