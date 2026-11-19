#!/usr/bin/env python3
"""
osu! Beat Sound  -  NMのままNC風のビート音を鳴らすアプリ

仕組み:
  tosu (osuのメモリリーダー) の WebSocket から「曲の再生位置」を受け取り、
  譜面(.osu)のタイミングポイントから計算したビート時刻に合わせて、
  sounds フォルダのサウンドを鳴らします。osu本体には一切触りません。

使い方:
  1. tosu を起動しておく  (https://github.com/KotRikD/tosu)
  2. sounds フォルダに kick / clap / hat / finish の wav/ogg/mp3 を入れる
  3. python nc_beat.py   (GUIなしなら  python nc_beat.py --nogui)
"""
import argparse, bisect, json, os, sys, threading, time, urllib.request, urllib.parse
from collections import deque

# exe化(PyInstaller)されている場合は exe のあるフォルダ、通常は .py のあるフォルダ
HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "tosu_url": "ws://127.0.0.1:24050/websocket/v2",
    "tosu_http": "http://127.0.0.1:24050",
    "sounds_dir": "sounds",
    "offset_ms": 0,            # +で早く鳴らす / -で遅く鳴らす (音声遅延の補正用)
    "volume": 0.8,
    "beats_on": True,          # ビート音のON/OFF (再生/停止ボタン)
    "always_on_top": False,    # ウィンドウを常に最前面に表示
    "minimize_console": True,  # 起動時に、黒いログ画面を最小化する (Windows)
    "smooth": True,            # 画面の円・ボタン・スイッチなどを、なめらかに (アンチエイリアス) 描く
    "start_fullscreen": False, # 起動時に、枠なし (タイトルバーを隠した状態) にする
    "hide_in_play": True,      # 「常に手前に表示」のとき、プレイ中だけ後ろへ下げる (一時停止・リザルト・選曲では、また手前に出る)
    "window_size": "",         # 前回のウィンドウの大きさ ("幅x高さ")
    "auto_launch_tosu": True,  # tosuが起動していなければ自動で起動する
    "tosu_path": "",           # tosu.exe の場所 (空なら自動で探す)
    "skin_dir": "",            # スキンフォルダを手動で指定する場合 (空ならtosuから自動取得)
    "theme": "dark",           # dark / light
    "accent": "#ff66aa",       # アクセントカラー
    "font_family": "Yu Gothic UI",
    "font_size": 10,
    "bpm_scale": 1.0,          # 0.5=半分 / 1=そのまま / 2=2倍 (160BPM譜面を320で刻みたい等)
    "enabled_sounds": {"kick": True, "clap": True, "hat": True, "finish": True},
    "play_states": ["play"],   # このゲーム状態のときだけ鳴らす (例: "play","selectplay" 等)
    "skip_if_nc": True,        # 本物のNCモッド中は二重に鳴らさない
    # 4/4拍子のパターン: 1小節=8ステップ(8分音符)。各サウンドを鳴らすステップ番号
    "meter_mode": "auto",      # "auto"=譜面の拍子に従う / "3","4","5","6","7"=拍子を手動指定
    "patterns": {},            # 拍子ごとのパターンを上書きしたい場合 例: {"3": {"kick":[0],"clap":[2,4],"hat":[1,3,5],"finish":[0]}}
    "language": "auto",        # auto / ja / en / lang フォルダの言語ファイル名
    "pattern_4_4": {
        "kick":   [0, 4],
        "clap":   [2, 6],
        "hat":    [1, 3, 5, 7],
        "finish": [0],
    },
}
SOUND_EXTS = (".wav", ".ogg", ".mp3", ".flac")
SOUND_ALIASES = {  # ファイル名の候補 (拡張子なし, 小文字)
    "kick":   ["kick", "nightcore-kick"],
    "clap":   ["clap", "nightcore-clap"],
    "hat":    ["hat", "nightcore-hat"],
    "finish": ["finish", "nightcore-finish"],
}
for _i in range(1, 9):                       # 数え声: beat1.wav (または count1 / 1) ... beat8。あれば、1拍ごとに 1 2 3 4 の順で鳴らす
    SOUND_ALIASES[f"beat{_i}"] = [f"beat{_i}", f"count{_i}", str(_i)]


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[config] 読み込み失敗: {e}")
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ───────────────────────── 譜面解析 ─────────────────────────
def parse_timing_points(text):
    """[TimingPoints] から 赤線(uninherited)だけ取り出す -> [(time_ms, beat_len_ms, meter)]"""
    pts, in_sec = [], False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_sec = line == "[TimingPoints]"
            continue
        if not in_sec or not line or line.startswith("//"):
            continue
        p = line.split(",")
        try:
            t, bl = float(p[0]), float(p[1])
            meter = int(p[2]) if len(p) > 2 else 4
            unin = int(p[6]) if len(p) > 6 else (1 if bl > 0 else 0)
        except ValueError:
            continue
        if unin == 1 and bl > 0:
            pts.append((t, bl, max(meter, 1)))
    pts.sort()
    return pts


def pattern_for(meter, patterns, pattern_4_4):
    """拍子ごとの鳴らし方 (8分音符のステップ番号)。1,3,5拍目=kick / 2,4,6拍目=clap / 裏拍=hat / 小節頭=finish"""
    if str(meter) in patterns:
        return patterns[str(meter)]
    if meter == 4:
        return pattern_4_4
    spm = meter * 2
    odd = meter % 2 == 1                      # 奇数拍子は最後の拍をclapにして、次の小節頭のkickと続かないようにする
    kick = list(range(0, spm - 2 if odd else spm, 4))
    clap = list(range(2, spm, 4)) + ([spm - 2] if odd else [])
    return {"kick": kick, "clap": clap, "hat": list(range(1, spm, 2)), "finish": [0]}


def build_events(pts, end_ms, pattern, scale=1.0, meter_override=None, patterns=None, counts=()):
    """ビートのイベント表を作る -> (times[], sounds[])  (ミリ秒昇順)
    meter_override: 拍子を手動指定 (None なら譜面の赤線の拍子に従う)"""
    patterns = patterns or {}
    times, sounds = [], []
    for i, (t0, bl, meter) in enumerate(pts):
        bl = bl / scale
        if meter_override:
            meter = int(meter_override)
        pat = pattern_for(meter, patterns, pattern)
        t_end = pts[i + 1][0] if i + 1 < len(pts) else end_ms
        step_len = bl / 2.0                      # 8分音符
        if step_len < 20.0:                      # 異常に速い赤線 (演出用の仕掛けなど): 鳴らさず、次の赤線からは続ける
            continue
        spm = meter * 2                          # 1小節のステップ数
        n = 0
        while True:
            t = t0 + n * step_len
            if t >= t_end - 1e-6 or n > 300000:  # 1つの赤線の内側だけを制限 (全体で打ち切ると、長い譜面の後半が鳴らなくなる)
                break
            s = n % spm
            names = [k for k, steps in pat.items() if s in steps]
            if counts and s % 2 == 0 and (s // 2 + 1) in counts:       # 数え声があれば、kick/clap の代わりに 1 2 3 4 を鳴らす
                names = [k for k in names if k not in ("kick", "clap")] + [f"beat{s // 2 + 1}"]
            if names:
                times.append(t)
                sounds.append(names)
            n += 1
    return times, sounds


# ───────────────────────── 音声 ─────────────────────────
class SoundBank:
    def __init__(self, folder):
        import pygame
        self.pg = pygame
        pygame.mixer.pre_init(44100, -16, 2, 256)  # 小さいバッファ = 低遅延
        pygame.mixer.init()
        pygame.mixer.set_num_channels(32)
        self.folder, self.sounds, self.volume = folder, {}, 0.8
        self.reload()

    def reload(self):
        self.sounds.clear()
        os.makedirs(self.folder, exist_ok=True)
        files = {os.path.splitext(f)[0].lower(): f for f in os.listdir(self.folder)
                 if f.lower().endswith(SOUND_EXTS)}
        for key, names in SOUND_ALIASES.items():
            for n in names:
                if n in files:
                    try:
                        self.sounds[key] = self.pg.mixer.Sound(os.path.join(self.folder, files[n]))
                        break
                    except Exception as e:
                        print(f"[sound] {files[n]} を読めません: {e}")
        self.set_volume(self.volume)
        return sorted(self.sounds)

    def loaded(self):
        return sorted(self.sounds)

    def set_volume(self, v):
        self.volume = v
        for s in self.sounds.values():
            s.set_volume(v)

    def play(self, key):
        s = self.sounds.get(key)
        if s:
            s.play()


# ───────────────────────── tosu 自動起動 / 自動起動登録 ─────────────────────────
TOSU_URL = "https://github.com/KotRikD/tosu/releases"


def tosu_running():
    if sys.platform != "win32":
        return False
    try:
        import subprocess
        # 日本語Windowsのtasklistはcp932で出力するため、文字コードに依存しないようバイト列で判定する
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq tosu.exe"], capture_output=True,
                             creationflags=0x08000000).stdout or b""
        return b"tosu.exe" in out.lower()
    except Exception:
        return False


def find_tosu(cfg):
    cands = [cfg.get("tosu_path", ""), os.path.join(HERE, "tosu.exe"), os.path.join(HERE, "tosu", "tosu.exe"),
             os.path.join(os.path.dirname(HERE), "tosu", "tosu.exe")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def launch_tosu(cfg):
    """tosuが動いていなければ起動する。起動した/すでに動いていれば True"""
    if tosu_running():
        return True
    path = find_tosu(cfg)
    if not path:
        return False
    try:
        import subprocess
        subprocess.Popen([path], cwd=os.path.dirname(path), creationflags=0x08000000 if sys.platform == "win32" else 0)
        return True
    except Exception as e:
        print(f"[tosu] 起動に失敗: {e}")
        return False


def _startup_file():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup", "osu_beat_sound.vbs")


def autostart_enabled():
    return sys.platform == "win32" and os.path.isfile(_startup_file())


def set_autostart(on):
    """Windows起動時に、黒い画面を出さずに自動起動する設定"""
    f = _startup_file()
    if not on:
        if os.path.isfile(f):
            os.remove(f)
        return
    if getattr(sys, "frozen", False):
        cmd = f'"""{sys.executable}"""'
    else:
        py = sys.executable
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        py = pyw if os.path.isfile(pyw) else py
        cmd = f'"""{py}"" ""{os.path.abspath(__file__)}"""'
    with open(f, "w", encoding="utf-8") as fp:
        fp.write('Set sh = CreateObject("WScript.Shell")\n')
        fp.write(f'sh.CurrentDirectory = "{HERE}"\n')
        fp.write(f"sh.Run {cmd}, 0, False\n")


def create_desktop_shortcut():
    """デスクトップに、このアプリを起動するショートカット (.lnk) を作る。作った場所を返す。Windows専用"""
    import subprocess
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    if getattr(sys, "frozen", False):
        target, args = sys.executable, ""
    else:
        py = sys.executable
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        target, args = (pyw if os.path.isfile(pyw) else py), f'"{os.path.abspath(__file__)}"'
    q = lambda t: t.replace("'", "''")
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$p=Join-Path $d 'osu! Beat Sound.lnk';"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
          f"$s.TargetPath='{q(target)}';$s.Arguments='{q(args)}';$s.WorkingDirectory='{q(HERE)}';"
          f"$s.IconLocation='{q(target)},0';$s.Save();Write-Output $p")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       creationflags=0x08000000, timeout=30)
    out = r.stdout.decode("utf-8", "ignore").strip().splitlines()
    if r.returncode != 0 or not out:
        raise RuntimeError((r.stderr.decode("utf-8", "ignore") or "failed").strip()[:200])
    return out[-1]


# ───────────────────────── なめらかな描画 (アンチエイリアス) ─────────────────────────
def _aa_rgb(col):
    """'#rrggbb' -> (r,g,b)。それ以外 (色名・空など) は None"""
    if isinstance(col, str) and len(col) == 7 and col[0] == "#":
        try:
            return int(col[1:3], 16), int(col[3:5], 16), int(col[5:7], 16)
        except ValueError:
            return None
    return None


def aa_build(kind, coords, fill, outline, width):
    """円・多角形・線を、4倍の大きさで描いてから縮めた、なめらかな画像にする -> (PIL画像, 置く位置x, y)。大きすぎれば None"""
    from PIL import Image, ImageDraw
    SS = 4
    R = getattr(Image, "Resampling", Image).LANCZOS
    xs, ys = coords[0::2], coords[1::2]
    ext = width / 2.0 + 1 if kind == "line" else (width / 2.0 if outline else 0) + 1
    x0, y0 = int(min(xs) - ext), int(min(ys) - ext)
    x1, y1 = int(max(xs) + ext + 0.999), int(max(ys) + ext + 0.999)
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    if w * h > 700 * 700:
        return None
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pt = lambda px, py: ((px - x0) * SS, (py - y0) * SS)
    f = (fill + (255,)) if fill else None
    if kind == "oval":
        a, b = pt(coords[0], coords[1]); c_, e = pt(coords[2], coords[3])
        if outline:                                    # 縁の線は、円の輪郭の上に、半分ずつかかる
            hw = width * SS / 2.0
            d.ellipse((a - hw, b - hw, c_ + hw - 1, e + hw - 1), fill=outline + (255,))
            d.ellipse((a + hw, b + hw, c_ - hw - 1, e - hw - 1), fill=f if f else (0, 0, 0, 0))   # 中は、色か、透明
        else:
            d.ellipse((a, b, c_ - 1, e - 1), fill=f)
    elif kind == "polygon":
        d.polygon([pt(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)], fill=f)
    else:
        d.line([pt(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)], fill=f, width=max(1, int(width * SS)), joint="curve")
    return im.resize((w, h), R), x0, y0


def make_aa_canvas_class(tk, enabled):
    """tk.Canvas を、円・多角形・太い線をなめらかに描くよう拡張したクラスを返す (enabled() が False なら、普通の描画)"""
    from collections import OrderedDict
    cache = OrderedDict()

    def get(key, builder):
        hit = cache.get(key)
        if hit is None:
            built = builder()
            if built is None:
                return None
            from PIL import ImageTk
            hit = (ImageTk.PhotoImage(built[0]), built[1], built[2])
            cache[key] = hit
            if len(cache) > 1200:
                cache.popitem(last=False)
        else:
            cache.move_to_end(key)
        return hit

    class AACanvas(tk.Canvas):
        def _aa(self, kind, args, kw, base):
            try:
                if not enabled() or set(kw) - {"fill", "outline", "width"}:
                    return base(*args, **kw)
                raw = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
                coords = [float(v) for v in raw]
                if len(coords) < (6 if kind == "polygon" else 4) or len(coords) % 2:
                    return base(*args, **kw)
                fill, outline = kw.get("fill") or None, kw.get("outline") or None
                rf, ro = _aa_rgb(fill) if fill else None, _aa_rgb(outline) if outline else None
                width = float(kw.get("width", 1))
                if (fill and rf is None) or (outline and ro is None):
                    return base(*args, **kw)
                if kind == "oval":
                    if "outline" not in kw and rf is not None:      # 縁を決めていない円は、Tkの標準 (黒い縁) のまま
                        return base(*args, **kw)
                    if rf is None and ro is None:
                        return base(*args, **kw)
                elif kind == "polygon":
                    if rf is None or ro is not None:
                        return base(*args, **kw)
                else:                                               # 線: 細い縦横の線は、そのままで十分きれい
                    if rf is None:
                        return base(*args, **kw)
                    straight = all(abs(coords[i] - coords[i - 2]) < 0.5 or abs(coords[i + 1] - coords[i - 1]) < 0.5 for i in range(2, len(coords), 2))
                    if width < 2 and straight:
                        return base(*args, **kw)
                key = (kind, tuple(round(c, 1) for c in coords), rf, ro, width)
                got = get(key, lambda: aa_build(kind, coords, rf, ro, width))
                if got is None:
                    return base(*args, **kw)
                img, x, y = got
                return self.create_image(x, y, image=img, anchor="nw")
            except Exception:
                return base(*args, **kw)

        def create_oval(self, *args, **kw):
            return self._aa("oval", args, kw, super().create_oval)

        def create_polygon(self, *args, **kw):
            return self._aa("polygon", args, kw, super().create_polygon)

        def create_line(self, *args, **kw):
            return self._aa("line", args, kw, super().create_line)

    return AACanvas


# ───────────────────────── エンジン ─────────────────────────
class Engine:
    def __init__(self, cfg, bank):
        self.cfg, self.bank = cfg, bank
        self.lock = threading.Lock()
        self.stop_flag = False
        self.status = {"conn": "conn.none", "map": "", "artist": "", "version": "", "map_ok": None,
                       "state": "-", "events": 0, "pos": 0}
        self.bg_bytes, self.bg_id = None, 0   # 譜面の背景画像 (画面表示用)
        self.bg_note = ""                      # 背景画像が取れなかった理由 (接続状態の画面に表示)
        # 譜面
        self.map_key = None
        self.times, self.sounds = [], []
        # 時刻推定
        self.samples = deque()      # (wall, live - wall*rate)
        self.est = None
        self.rate = 1.0
        self.live = 0.0
        self.last_live = None
        self.last_change_wall = 0.0
        self.paused = True
        self.active = False         # 鳴らして良い状態か
        self.last_pos = None
        self.folders = {}           # tosuから受け取ったフォルダ情報 (スキンの場所など)

    # ---- tosu からのメッセージ処理 ----
    def on_message(self, data):
        now = time.perf_counter()
        state = str(data.get("state", {}).get("name", "")).lower()
        mods = str(data.get("play", {}).get("mods", {}).get("name", "")).upper()
        mods_rate = data.get("play", {}).get("mods", {}).get("rate")
        rate = float(mods_rate) if isinstance(mods_rate, (int, float)) and mods_rate else 1.0
        if mods_rate is None:
            tokens = [mods[i:i + 2] for i in range(0, len(mods), 2)]
            rate = 1.5 if ("DT" in tokens or "NC" in tokens) else 0.75 if "HT" in tokens else 1.0
        bm = data.get("beatmap", {})
        live = float(bm.get("time", {}).get("live", 0))
        allowed = state in [s.lower() for s in self.cfg["play_states"]]
        is_nc = "NC" in [mods[i:i + 2] for i in range(0, len(mods), 2)]
        with self.lock:
            self.status["state"] = state or "-"
            self.folders = data.get("folders") or self.folders
            self.active = allowed and not (self.cfg["skip_if_nc"] and is_nc)
            # 譜面が変わったら読み込む
            key = (bm.get("checksum") or bm.get("id"), bm.get("title"), data.get("files", {}).get("beatmap"))
            if key != self.map_key:
                self.map_key = key
                self.times, self.sounds = [], []
                self.last_pos = None
                self.status.update(map=bm.get("title") or "", artist=bm.get("artist") or "",
                                   version=bm.get("version") or "", map_ok=None, events=0)
                self.bg_bytes, self.bg_id = None, self.bg_id + 1
                threading.Thread(target=self.load_map, args=(data, key), daemon=True).start()
            # 時刻の推定 (遅れて届いたメッセージの影響を、窓内の最大値で打ち消す)
            if rate != self.rate:
                self.rate = rate
                self.samples.clear(); self.est = None
            if self.last_live is not None and live != self.last_live:
                self.last_change_wall = now
                self.paused = False
            self.last_live = live
            if now - self.last_change_wall > 0.2:
                self.paused = True
            self.live = live
            # 譜面の長さが短めに報告されたり、読み込み時に分からなかった場合でも、ビート表が途中で終わらないように、
            # 再生位置の2分先まで常に用意しておく (足りなくなったら、10分分を作り足す)
            if getattr(self, "pts", None) and live + 120000.0 > self.end:
                self.end = live + 600000.0
                self._rebuild_locked(keep_pos=True)
            off = live - now * 1000.0 * self.rate
            if self.est is not None and abs(off - self.est) > 250:   # シーク/リトライ
                self.samples.clear(); self.est = None; self.last_pos = None
            self.samples.append((now, off))
            while self.samples and now - self.samples[0][0] > 1.5:
                self.samples.popleft()
            target = max(o for _, o in self.samples)
            # 推定値は少しずつ寄せる (一気に動かすと、拍が重なったり詰まったりして、リズムが不揃いに聞こえる)
            if self.est is None or abs(target - self.est) > 40:
                self.est = target
            else:
                self.est += max(-1.0, min(2.0, target - self.est))

    def load_map(self, data, key):
        try:
            text = self.read_osu(data)
            pts = parse_timing_points(text)
            end = self.map_end(data)
            times, sounds = build_events(pts, end, self.cfg["pattern_4_4"], self.cfg["bpm_scale"], self._meter(), self.cfg["patterns"], self._counts())
            with self.lock:
                if self.map_key == key:
                    self.pts, self.end = pts, end
                    self.times, self.sounds = times, sounds
                    self.status["events"] = len(times)
                    self.status["map_ok"] = True
            print(f"[map] {self.status['map']}  赤線={len(pts)}  イベント={len(times)}")
        except Exception as e:
            print(f"[map] 譜面の読み込みに失敗: {e}")
            with self.lock:
                if self.map_key == key:
                    self.status["map_ok"] = False
        self.fetch_bg(data, key)

    def fetch_bg(self, data, key):
        """背景画像を読み込む (失敗しても無視。画面側は代わりの表示を出す)"""
        self._bg_tried = []
        try:
            raw = self.read_bg(data)
        except Exception as e:
            raw = None
            self._bg_tried.append(f"error: {e}")
        note = "ok" if raw else "; ".join(self._bg_tried[:4]) or "no path from tosu"
        print(f"[bg] {'OK ' + str(len(raw)) + ' bytes' if raw else 'NG ' + note}")
        with self.lock:
            if self.map_key == key:
                self.bg_bytes, self.bg_id, self.bg_note = raw, self.bg_id + 1, note

    def read_bg(self, data):
        fo, fi, dp = data.get("folders", {}), data.get("files", {}), data.get("directPath", {})
        cands = []
        if dp.get("beatmapBackground"):
            cands += [dp["beatmapBackground"], os.path.join(fo.get("songs", ""), dp["beatmapBackground"])]
        if fi.get("background"):
            cands.append(os.path.join(fo.get("songs", ""), fo.get("beatmap", ""), fi["background"]))
        self._bg_tried.extend(c for c in cands if c)
        if not cands and not fi.get("background"):
            self._bg_tried.append("tosu returned no background path")
        for c in cands:
            if c and os.path.isfile(c) and os.path.getsize(c) < 16_000_000:
                with open(c, "rb") as f:
                    return f.read()
        for rel in filter(None, [dp.get("beatmapBackground"), os.path.join(fo.get("beatmap", ""), fi.get("background", "")) if fi.get("background") else ""]):
            url = self.cfg["tosu_http"] + "/files/beatmap/" + urllib.parse.quote(rel.replace("\\", "/"))
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    return r.read(16_000_000)
            except Exception as e:
                self._bg_tried.append(f"{url} -> {e}")
        return None

    @staticmethod
    def map_end(data):
        """ビートを作る終わりの時刻。音の長さと最後のノーツの、長い方 + 1分 (短めに報告されても止まらないように)。不明なら30分"""
        tm = data.get("beatmap", {}).get("time", {}) or {}
        vals = []
        for k in ("mp3Length", "lastObject"):
            try:
                vals.append(float(tm.get(k) or 0))
            except (TypeError, ValueError):
                pass
        top = max(vals) if vals else 0.0
        return top + 60000.0 if top > 0 else 1800000.0

    def read_osu(self, data):
        candidates = []
        dp = data.get("directPath", {}).get("beatmapFile")
        if dp:
            candidates.append(dp)
            candidates.append(os.path.join(data.get("folders", {}).get("songs", ""), dp))
        fo, fi = data.get("folders", {}), data.get("files", {})
        if fi.get("beatmap"):
            candidates.append(os.path.join(fo.get("songs", ""), fo.get("beatmap", ""), fi["beatmap"]))
        for p in candidates:
            if p and os.path.isfile(p):
                with open(p, encoding="utf-8-sig", errors="replace") as f:
                    return f.read()
        # ローカルで見つからなければ tosu のHTTP経由で取得
        if dp or fi.get("beatmap"):
            for rel in filter(None, [dp, os.path.join(fo.get("beatmap", ""), fi.get("beatmap", ""))]):
                url = self.cfg["tosu_http"] + "/files/beatmap/" + urllib.parse.quote(rel.replace("\\", "/"))
                try:
                    with urllib.request.urlopen(url, timeout=3) as r:
                        return r.read().decode("utf-8-sig", "replace")
                except Exception:
                    pass
        raise FileNotFoundError("譜面ファイルが見つかりません")

    def _meter(self):
        m = str(self.cfg.get("meter_mode", "auto"))
        return int(m) if m.isdigit() else None

    def _rebuild_locked(self, keep_pos=False):
        if not getattr(self, "pts", None):
            return
        self.times, self.sounds = build_events(self.pts, self.end, self.cfg["pattern_4_4"], self.cfg["bpm_scale"], self._meter(), self.cfg["patterns"], self._counts())
        self.status["events"] = len(self.times)
        if not keep_pos:
            self.last_pos = None

    def _counts(self):
        """鳴らせる数え声 (beat1..beat8) の番号。「数え声」がOFFなら空"""
        if not self.cfg["enabled_sounds"].get("count", True):
            return ()
        return {i for i in range(1, 9) if f"beat{i}" in getattr(self.bank, "sounds", {})}

    def rebuild(self):
        """BPM倍率が変わったときにビート表を作り直す"""
        with self.lock:
            self._rebuild_locked()

    # ---- 再生ループ ----
    def current_pos(self, now):
        if self.est is None:
            return None
        if self.paused:
            return self.live
        return self.est + now * 1000.0 * self.rate

    def play_loop(self):
        # 他の処理 (画面の描画など) に邪魔されて、拍が遅れたり固まったりしないようにする
        try:
            sys.setswitchinterval(0.001)
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)          # sleep(0.001) を、本当に約1msにする
        except Exception:
            pass
        while not self.stop_flag:
            try:
                self._play_once()
            except Exception as e:                  # 何かあっても、ビートの処理を止めない
                print(f"[play] {e!r}")
                time.sleep(0.05)

    def _play_once(self):
        if True:
            now = time.perf_counter()
            with self.lock:
                pos = self.current_pos(now)
                if pos is not None:
                    pos += self.cfg["offset_ms"] * self.rate
                times, sounds = self.times, self.sounds
                active = self.active and self.cfg.get("beats_on", True)
                due = []
                if pos is not None:
                    self.status["pos"] = pos
                    lp = self.last_pos
                    if lp is not None and -300 < pos - lp < 0:
                        pos = lp                    # 位置は戻さない (戻すと、同じ拍が二重に鳴る)
                    if lp is not None and active and not self.paused and times and 0 <= pos - lp < 300:
                        i, j = bisect.bisect_right(times, lp), bisect.bisect_right(times, pos)
                        for k in range(i, j):
                            due.extend(sounds[k])
                    self.last_pos = pos
            for name in due:
                if self.cfg["enabled_sounds"].get("count" if name.startswith("beat") else name, True):
                    self.bank.play(name)
            time.sleep(0.001)

    # ---- WebSocket ----
    def ws_loop(self):
        import websocket
        while not self.stop_flag:
            try:
                self.status["conn"] = "conn.connecting"
                ws = websocket.create_connection(self.cfg["tosu_url"], timeout=5)
                ws.settimeout(2)
                self.status["conn"] = "conn.ok"
                while not self.stop_flag:
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not msg:
                        break
                    try:
                        self.on_message(json.loads(msg))
                    except Exception as e:
                        print("[msg]", repr(e))
                ws.close()
            except Exception as e:
                self.status["conn"] = "conn.fail"
                time.sleep(2)

    def start(self):
        if sys.platform == "win32":
            try:
                import ctypes; ctypes.windll.winmm.timeBeginPeriod(1)  # sleep精度を1msに
            except Exception:
                pass
        threading.Thread(target=self.play_loop, daemon=True).start()
        threading.Thread(target=self.ws_loop, daemon=True).start()


# ───────────────────────── 多言語 (i18n) ─────────────────────────
LANG_NAMES = {"ja": "日本語", "en": "English"}
STR_EN = {
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    "tab.nc": "NC Beats", "tab.set": "Setting", "menu.full": "Fullscreen (F11)", "menu.quit": "Quit",
    "head.status": "Status", "head.adjust": "Adjust", "head.sounds": "Sounds",
    
    "head.design": "Appearance", "head.behavior": "Behavior",
    "lbl.tosu": "tosu", "lbl.state": "Game state", "lbl.map": "Beatmap", "lbl.loaded": "Loaded sounds",
    "lbl.volume": "Volume", "lbl.timing": "Timing", "lbl.bpm": "BPM scale", "lbl.meter": "Time sig.",
    "lbl.theme": "Theme", "lbl.accent": "Accent color", "lbl.font": "Font",
    "lbl.fontsize": "Font size", "lbl.language": "Language",
    "chk.select": "Also play on the song select screen", "chk.top": "Always on top",
    "chk.autostart": "Start with Windows", "btn.shortcut": "Create desktop shortcut", "msg.shortcut": "Shortcut created on the desktop:\n{0}", "err.shortcut": "Could not create the shortcut: {0}", "chk.tosu": "Launch tosu automatically",
    "btn.reload_sounds": "Reload sounds", 
    "btn.apply": "Apply", "btn.restore": "Restore originals",
    "btn.open_dir": "Open sounds folder", "btn.reload": "Rescan", "btn.pick_tosu": "Locate tosu.exe",
    "btn.dl_tosu": "Download tosu",
    "theme.dark": "Dark", "theme.light": "Light",
    "accent.pink": "Pink", "accent.blue": "Blue", "accent.green": "Green", "accent.purple": "Purple", "accent.orange": "Orange",
    "lang.auto": "Auto",
    "meter.auto": "Follow the beatmap (auto)",
    "meter.hint": "The beatmap's own time signature (3/4, 7/4 ...) is followed automatically. If a map says 4/4 but the song is in another meter, choose it here.",
    "conn.none": "Not connected", "conn.connecting": "Connecting...", "conn.ok": "Connected",
    "conn.fail": "Cannot reach tosu (is it running?)",
    "map.fail": "Failed to load", "map.fmt": "{0} ({1} beats)",
    "snd.none": "None (put files in the sounds folder)", "snd.count": "count (beat1-8)", "snd.pt": "pt",
    
    "lbl.auto": "Auto (tosu)", "hint.palette": "Drag to choose",
    
    "chk.smooth": "Smooth drawing (anti-aliasing)",
    "chk.hideplay": "Hide behind the game while playing (shows again on pause / results)", "lbl.winsize": "Window size", "size.fit": "Fit screen height",
    "chk.mincon": "Minimize the black log window at startup",
    "chk.startfull": "Start in fullscreen (no title bar; drag the edges to resize)",
    
    
    
    
    
    
    
    "btn.ok": "OK", "btn.cancel": "Cancel",
    
    "msg.pickset": "Please choose a set.",
    
    
    "msg.norestore": "Nothing to restore (no changes yet).",
    
    "msg.restore_title": "Restore",
    "msg.restore_ask": "This restores every sound changed by this app to its original state. Continue?",
    
    "msg.nocand": "No candidates yet. Click \"Open sounds folder\" and put sounds in the parts folder (see README.txt).",
    "err.title": "Error", "err.switch": "Failed to switch: {0}", "err.restore": "Failed to restore: {0}",
    "err.autostart": "Failed to set auto-start: {0}",
    "dlg.picktosu": "Choose tosu.exe",
    "player.from": "Playing from beatmap", "player.notitle": "Waiting for a beatmap...",
    "state.play": "Playing", "state.selectplay": "Song select", "state.menu": "Menu",
    "lbl.sounds": "Sounds", "lbl.onoff": "Beat sound", "onoff.on": "ON", "onoff.off": "OFF",
    "dlg.conn": "Connection", "dlg.sounds": "Sound settings",
    "lbl.bg": "Background", "bg.ok": "OK ({0}x{1})", "bg.nopil": "Pillow is not installed. Run: python -m pip install pillow",
    "bg.nofile": "Image not found: {0}", "bg.bad": "Could not read the image file", "bg.wait": "Waiting for a beatmap...",
}
STR_JA = {
    "head.status": "状態", "head.adjust": "調整", "head.sounds": "鳴らす音",
    
    "head.design": "デザイン", "head.behavior": "動作",
    "lbl.state": "ゲーム状態", "lbl.map": "譜面", "lbl.loaded": "読込済みの音",
    "lbl.volume": "音量", "lbl.timing": "タイミング", "lbl.bpm": "BPM倍率", "lbl.meter": "拍子",
    "lbl.theme": "テーマ", "lbl.accent": "アクセントカラー", "lbl.font": "フォント",
    "lbl.fontsize": "文字の大きさ", "lbl.language": "言語",
    "chk.select": "曲選択画面でも鳴らす", "chk.top": "常に最前面に表示",
    "chk.autostart": "Windows起動時に自動で起動する", "btn.shortcut": "デスクトップにショートカットを作る", "msg.shortcut": "デスクトップにショートカットを作りました:\n{0}", "err.shortcut": "ショートカットを作れませんでした: {0}", "chk.tosu": "tosuを自動で起動する",
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    "btn.reload_sounds": "サウンドを再読み込み", 
    "btn.apply": "適用", "btn.restore": "元に戻す",
    "btn.open_dir": "音のフォルダを開く", "btn.reload": "再読み込み", "btn.pick_tosu": "tosu.exeの場所を指定",
    "btn.dl_tosu": "tosuをダウンロード",
    "theme.dark": "ダーク", "theme.light": "ライト",
    "accent.pink": "ピンク", "accent.blue": "ブルー", "accent.green": "グリーン", "accent.purple": "パープル", "accent.orange": "オレンジ",
    "lang.auto": "自動",
    "meter.auto": "譜面に合わせる（自動）",
    "meter.hint": "譜面が3/4や7/4などを指定している場合は、自動で合わせます。譜面が4/4のままでも曲が変拍子のときは、ここで選んでください。",
    "conn.none": "未接続", "conn.connecting": "接続中...", "conn.ok": "接続OK",
    "conn.fail": "tosuに接続できません (起動してますか?)",
    "map.fail": "読み込み失敗", "map.fmt": "{0} ({1}拍)",
    "snd.none": "なし (soundsフォルダに入れてください)", "snd.count": "数え声 (beat1〜8)",
    
    "lbl.auto": "自動（tosu）", "hint.palette": "なぞって選ぶ",
    
    "chk.smooth": "なめらかに描画（アンチエイリアス）",
    "chk.hideplay": "プレイ中は後ろに隠す（一時停止・終了で再表示）", "lbl.winsize": "ウィンドウの大きさ", "size.fit": "画面の高さに合わせる",
    "chk.mincon": "起動時に黒いログ画面を最小化する",
    "chk.startfull": "フルスクリーン（枠なし）で起動（端をドラッグで大きさ変更）",
    
    
    
    
    
    
    
    "btn.ok": "OK", "btn.cancel": "キャンセル",
    
    "msg.pickset": "セットを選んでください。",
    
    
    "msg.norestore": "元に戻すものはありません（まだ切り替えていません）。",
    
    "msg.restore_title": "元に戻す",
    "msg.restore_ask": "このアプリで切り替えた音を、すべて元の状態に戻します。よろしいですか？",
    
    "msg.nocand": "候補がありません。「音のフォルダを開く」から、parts フォルダに音を入れてください（説明は README.txt にあります）。",
    "err.title": "エラー", "err.switch": "切り替えに失敗しました: {0}", "err.restore": "復元に失敗しました: {0}",
    "err.autostart": "自動起動の設定に失敗しました: {0}",
    "dlg.picktosu": "tosu.exe を選んでください",
    "player.from": "譜面から再生中", "player.notitle": "譜面を待っています...",
    "state.play": "プレイ中", "state.selectplay": "曲選択", "state.menu": "メニュー",
    "lbl.sounds": "鳴らす音", "lbl.onoff": "ビート音", "onoff.on": "ON", "onoff.off": "OFF",
    "dlg.conn": "接続状態", "dlg.sounds": "鳴らす音の設定", "menu.full": "フルスクリーン (F11)", "menu.quit": "アプリを終了",
    "lbl.bg": "背景画像", "bg.ok": "OK ({0}x{1})", "bg.nopil": "Pillowが入っていません。次を実行してください: python -m pip install pillow",
    "bg.nofile": "画像が見つかりません: {0}", "bg.bad": "画像ファイルを読み込めません", "bg.wait": "譜面を待っています...",
}
LANGS = {"en": dict(STR_EN), "ja": {**STR_EN, **STR_JA}}
_lang = {"code": "en"}


def load_lang_files():
    """lang フォルダの *.json を翻訳として読み込む。 {"_name": "Español", "キー": "文字列", ...}
    足りないキーは英語で表示される。"""
    d = os.path.join(HERE, "lang")
    if not os.path.isdir(d):
        return
    for f in sorted(os.listdir(d)):
        if f.lower().endswith(".json"):
            code = os.path.splitext(f)[0].lower()
            try:
                with open(os.path.join(d, f), encoding="utf-8-sig") as fp:
                    data = json.load(fp)
                LANG_NAMES[code] = str(data.pop("_name", code))
                LANGS[code] = {**STR_EN, **{k: str(v) for k, v in data.items()}}
            except Exception as e:
                print(f"[lang] {f} を読めません: {e}")


def detect_language():
    """OSの表示言語から、auto のときの言語を決める (日本語なら ja、それ以外は en)"""
    try:
        if sys.platform == "win32":
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            return "ja" if lid == 0x11 else "en"
        import locale
        loc = (locale.getlocale()[0] or "").lower()
        return "ja" if loc.startswith("ja") else "en"
    except Exception:
        return "en"


def set_language(code):
    if code == "auto" or code not in LANGS:
        code = detect_language()
    _lang["code"] = code if code in LANGS else "en"


def tr(key, *args):
    s = LANGS.get(_lang["code"], {}).get(key)
    if s is None:
        s = STR_EN.get(key, key)
    return s.format(*args) if args else s


# ───────────────────────── GUI ─────────────────────────
THEMES = {
    "dark":  {"bg": "#1c1c22", "fg": "#ececf2", "panel": "#2b2b35", "field": "#34343f", "muted": "#9a9aab"},
    "light": {"bg": "#f3f3f7", "fg": "#1b1b22", "panel": "#e2e2ea", "field": "#ffffff", "muted": "#666672"},
}
ACCENTS = {"accent.pink": "#ff66aa", "accent.blue": "#3d8bfd", "accent.green": "#2fbf85",
           "accent.purple": "#9b7bff", "accent.orange": "#ff8a3d"}
METERS = ["auto", "4", "3", "5", "6", "7"]
APP_VERSION = "1.1.0"
APP_TITLE = f"osu! Beat Sound v{APP_VERSION}"


# ───────────────────────── プレイヤー画面の描画 ─────────────────────────
PLAYER_MUTED = {"dark": "#c9c9d6", "light": "#4a4a57"}


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in rgb)


def _unhex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    """色a→bを t(0〜1) の割合で混ぜる"""
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def dominant_color(img):
    """画像の代表色 (彩度の高い色を重く見た平均)。 -> (r, g, b)"""
    import colorsys
    small = img.convert("RGB").resize((24, 24))
    tot = [0.0, 0.0, 0.0]; wsum = 0.0
    for r, g, b in small.getdata():
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        w = 0.05 + s * v
        tot[0] += r * w; tot[1] += g * w; tot[2] += b * w; wsum += w
    return tuple(t / wsum for t in tot)


def _round_mask(size, radius):
    from PIL import Image, ImageDraw
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    try:
        d.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    except AttributeError:
        d.rectangle((0, 0, size[0] - 1, size[1] - 1), fill=255)
    return m


def build_player_bg(w, h, rect, cover_src, accent, theme, draw_cover=True, halo_only=False):
    """背景画像を作る: 譜面画像の色のグラデーション + ジャケットの後ろからの光 + 影 + ジャケット。
    rect=(x0,y0,x1,y1) ジャケットの位置。cover_src が None なら、アクセントカラーで代用して、ジャケットは描かない。"""
    import colorsys
    from PIL import Image, ImageDraw, ImageFilter
    x0, y0, x1, y1 = [int(v) for v in rect]
    cw, ch = x1 - x0, y1 - y0
    base = dominant_color(cover_src) if cover_src is not None else _unhex(accent)
    hh, ss, vv = colorsys.rgb_to_hsv(*(c / 255 for c in base))
    ss = max(0.35, min(0.92, ss * 1.15 + 0.05))
    hsv = lambda s_, v_: tuple(c * 255 for c in colorsys.hsv_to_rgb(hh, max(0, min(1, s_)), max(0, min(1, v_))))
    dark = theme != "light"
    if dark:
        top, bottom, glow = hsv(ss, 0.52), hsv(ss * 0.85, 0.10), hsv(ss * 0.65, 1.0)
        glow_a, halo_a, shadow_a = 0.95, 0.50, 0.40
    else:
        top, bottom, glow = hsv(ss * 0.45, 0.97), hsv(ss * 0.12, 0.99), hsv(ss * 0.9, 1.0)
        glow_a, halo_a, shadow_a = 0.80, 0.40, 0.22
    # 縦方向のグラデーション (ゆるやかに)
    grad = Image.linear_gradient("L").resize((w, h))
    grad = grad.point(lambda v: int(255 * ((v / 255) ** 0.8)))
    img = Image.composite(Image.new("RGB", (w, h), _hex(bottom)), Image.new("RGB", (w, h), _hex(top)), grad)

    def blob(rc, blur, alpha, color, grow=0):
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).rectangle((rc[0] - grow, rc[1] - grow, rc[2] + grow, rc[3] + grow), fill=255)
        m = m.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * alpha))
        return Image.composite(Image.new("RGB", (w, h), _hex(color)), img, m)

    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    img = blob((cx - cw, cy - ch // 2 - 30, cx + cw, cy + ch // 2 + 30), 70, halo_a, glow)        # 大きな後光
    if not halo_only:
        img = blob((x0, y0, x1, y1), 26, glow_a, glow, grow=8)                                   # ジャケットの縁の光
        img = blob((x0 + 6, y0 + 14, x1 - 6, y1 + 12), 12, shadow_a, (0, 0, 0) if dark else (40, 30, 60))  # 足元の影
    # 上下の端をぼかして、端の色になじませる (ジャケットより前に処理するので、ジャケットはぼけない)
    top_h, bot_h = 70, 110
    ramp = Image.new("L", (1, h), 0)
    for yy in range(h):
        a = max(0.0, 1 - yy / top_h) ** 1.6 if yy < top_h else (max(0.0, 1 - (h - 1 - yy) / bot_h) ** 1.6 if yy > h - bot_h else 0.0)
        ramp.putpixel((0, yy), int(255 * a))
    ramp = ramp.resize((w, h))
    blurred = img.filter(ImageFilter.GaussianBlur(14))
    fade = Image.new("RGB", (w, h), _hex(_mix(top, (0, 0, 0), 0.25)) if dark else _hex(_mix(top, (255, 255, 255), 0.4)))
    edge_img = Image.composite(blurred, img, ramp)
    img = Image.composite(Image.blend(edge_img, fade, 0.35), img, ramp)
    if not draw_cover:                                                                             # スライド中は、ジャケットだけ別に動かす
        pass
    elif cover_src is None:                                                                        # 画像が無いときのタイル
        tile = Image.new("RGB", (cw, ch), _hex(_mix(top, glow, 0.30 if dark else 0.45)))
        img.paste(tile, (x0, y0), _round_mask((cw, ch), 10))
    else:
        from PIL import ImageOps
        cov = ImageOps.fit(cover_src.convert("RGB"), (cw, ch), getattr(Image, "Resampling", Image).LANCZOS)
        img.paste(cov, (x0, y0), _round_mask((cw, ch), 10))
        edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))                                            # 淡い縁取り
        try:
            ImageDraw.Draw(edge).rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=10, outline=(255, 255, 255, 60), width=1)
        except AttributeError:
            pass
        img = Image.alpha_composite(img.convert("RGBA"), edge).convert("RGB")
    return img


TRANSPARENT_KEY = "#010101"      # 枠なしのとき、この色の部分を透明にして角を丸く見せる


def round_corners(im, radius):
    """枠なしのとき、角の外側を「透明にする色」で塗る"""
    if not radius:
        return im
    from PIL import Image, ImageDraw
    W, h = im.size
    k = 3
    m = Image.new("L", (W * k, h * k), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, W * k - 1, h * k - 1), radius * k, fill=255)
    m = m.resize((W, h), getattr(Image, "Resampling", Image).LANCZOS).point(lambda v: 255 if v > 127 else 0)
    return Image.composite(im.convert("RGB").point(lambda v: max(v, 4)), Image.new("RGB", (W, h), TRANSPARENT_KEY), m)


def palette_color(u, v):
    """パレット上の位置 (u=色相 0〜1, v=上から下へ 0〜1) -> '#rrggbb'。上は淡い色、中央は鮮やかな色、下は暗い色"""
    import colorsys
    u, v = max(0.0, min(1.0, u)), max(0.0, min(1.0, v))
    if v < 0.5:
        sat, val = 0.22 + 0.78 * (v * 2), 1.0
    else:
        sat, val = 1.0, 1.0 - 0.72 * ((v - 0.5) * 2)
    r, g, b = colorsys.hsv_to_rgb(u * 0.999, sat, val)
    return "#%02x%02x%02x" % (int(r * 255 + .5), int(g * 255 + .5), int(b * 255 + .5))


_pal_cache = {}


def palette_image(w, h):
    from PIL import Image
    k = (w, h)
    if k not in _pal_cache:
        sw, sh = max(2, w // 3), max(2, h // 3)
        im = Image.new("RGB", (sw, sh))
        px = im.load()
        for yy in range(sh):
            for xx in range(sw):
                col = palette_color(xx / max(1, sw - 1), yy / max(1, sh - 1))
                px[xx, yy] = (int(col[1:3], 16), int(col[3:5], 16), int(col[5:7], 16))
        _pal_cache[k] = im.resize((w, h), getattr(Image, "Resampling", Image).BICUBIC)
    return _pal_cache[k]


def frost_panel(bg_img, rect, dark, radius=18, pad=24):
    """半透明のすりガラス風パネル (丸い角 + 影 + 淡い縁)。背景の同じ場所を、ぼかして色をのせて作る。RGBA画像"""
    from PIL import Image, ImageDraw, ImageFilter
    S = getattr(Image, "Resampling", Image).LANCZOS
    x0, y0, x1, y1 = [int(v) for v in rect]
    pw, ph = x1 - x0, y1 - y0
    crop = bg_img.crop((x0, y0, x1, y1)).convert("RGB").filter(ImageFilter.GaussianBlur(8))
    crop = Image.blend(crop, Image.new("RGB", (pw, ph), (0, 0, 0) if dark else (255, 255, 255)), 0.45 if dark else 0.55)
    k = 3
    m = Image.new("L", (pw * k, ph * k), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, pw * k - 1, ph * k - 1), radius * k, fill=236)       # 少しだけ透ける
    m = m.resize((pw, ph), S)
    full = Image.new("RGBA", (pw + 2 * pad, ph + 2 * pad), (0, 0, 0, 0))
    sh = Image.new("L", full.size, 0)
    ImageDraw.Draw(sh).rounded_rectangle((pad, pad + 8, pad + pw, pad + ph + 8), radius, fill=120)
    shadow = Image.new("RGBA", full.size, (0, 0, 0, 0))
    shadow.putalpha(sh.filter(ImageFilter.GaussianBlur(12)))
    full = Image.alpha_composite(full, shadow)
    panel = crop.convert("RGBA")
    panel.putalpha(m)
    edge = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, pw - 1, ph - 1), radius, outline=(255, 255, 255, 80 if dark else 140), width=1)
    full.alpha_composite(Image.alpha_composite(panel, edge), (pad, pad))
    return full


def wrap_text(measure, text, size, bold, maxw, lines=2):
    """幅 maxw に収まらない文字を、最大 lines 行に折り返す (英単語は途中で切らない / 日本語は1文字ずつ)。入りきらない分は「…」"""
    text = str(text)
    if measure(text, size, bold) <= maxw:
        return [text]
    import re
    toks = re.findall(r"[A-Za-z0-9_'’.,:;/\\\-()!?%+]+\s*|\s+|.", text)
    out, cur = [], ""
    for t in toks:
        if cur and measure((cur + t).rstrip(), size, bold) > maxw and len(out) < lines - 1:
            out.append(cur.rstrip()); cur = t.lstrip()
        else:
            cur += t
    out.append(cur.rstrip())
    last = out[-1]
    while len(last) > 1 and measure(last, size, bold) > maxw:
        last = last[:-2] + "…"
    out[-1] = last
    return out


def draw_text2(c, measure, fam, x, y, text, size, bold, maxw, anchor, fill, lines=2):
    """1行に収まればそのまま、収まらなければ2行にして、y を中心に描く。行数を返す"""
    ls = wrap_text(measure, text, size, bold, maxw, lines)
    lh = int(size * 1.7)
    y0 = y - (len(ls) - 1) * lh / 2
    for i, ln in enumerate(ls):
        c.create_text(x, y0 + i * lh, anchor=anchor, text=ln, fill=fill, font=(fam, size, "bold") if bold else (fam, size))
    return len(ls)


class OverlayMixin:
    """画面の上に重ねて出す、半透明・丸い角のパネル。種類: menu(項目の選択) / info(情報の表示) / drop(ドロップ) / input(名前の入力)。
    パネルは、上から下へ伸びるように出てくる。パネルの外を押すと閉じる。"""
    ov = None
    ov_hits = ()
    OV_PAD = 24

    def ov_open(self, kind, title, rows=None, anchor=None, on_pick=None, **kw):
        self.ov = {"kind": kind, "title": title, "rows": rows or [], "anchor": anchor or ("center",), "pick": on_pick,
                   "t0": time.time(), "key": None, "photo": None, "scroll": 0, "max_scroll": 0}
        self.ov.update(kw)
        self.hover = None
        self.redraw()
        self.c.after(16, self._ov_tick)

    def ov_close(self):
        self.ov, self.ov_hits = None, ()
        self.redraw()

    def _ov_p(self):
        if not self.ov:
            return 1.0
        t = max(0.0, min(1.0, (time.time() - self.ov["t0"]) / 0.24))
        return 1 - (1 - t) ** 3

    def _ov_tick(self):
        if self.ov and self._ov_p() < 1.0:
            self.redraw()
            self.c.after(16, self._ov_tick)
        elif self.ov:
            self.redraw()

    def ov_hit(self, x, y):
        for h_ in reversed(self.ov_hits):
            if h_[0] <= x <= h_[2] and h_[1] <= y <= h_[3]:
                return h_
        return None

    def _ov_pick(self, row):
        ov = self.ov
        fn = ov.get("pick") if ov else None
        res = fn(row) if fn else None
        if self.ov is ov:                      # 選んだ処理が別のパネルを開いていなければ
            if isinstance(res, list):
                ov["rows"] = res
                self.redraw()
            elif res == "keep" or row.get("keep"):
                self.redraw()
            else:
                self.ov_close()

    def ov_click(self, e):
        h_ = self.ov_hit(e.x, e.y)
        if h_ is None:
            self.ov_close()
            return
        name = h_[4]
        if name == "ovrow":
            self._ov_pick(self.ov["rows"][h_[5]])
        elif name == "ovprev":
            try:
                self.ov["rows"][h_[5]]["preview"]()
            except Exception:
                pass
        elif name == "ovbtn":
            self._ov_pick({"key": h_[5], "btn": True})
        elif name == "ovpal":
            self.ov["drag"] = True
            self.ov_move(e)
        elif name == "ovsw":
            self.ov["color"] = h_[5]; self.ov["mark"] = None
            fn = self.ov.get("pick")
            if fn:
                fn({"color": h_[5], "final": True})
            self.redraw()

    def ov_move(self, e):
        """カラーパレットの上をなぞると、色が変わる (離したときに、実際に反映する)"""
        ov = self.ov
        if not ov or ov.get("kind") != "color" or not ov.get("drag"):
            return
        pr = ov.get("pal_rect")
        if not pr:
            return
        x0, y0, x1, y1 = pr
        x = max(x0, min(x1 - 1, e.x)); y = max(y0, min(y1 - 1, e.y))
        ov["color"] = palette_color((x - x0) / max(1, x1 - x0 - 1), (y - y0) / max(1, y1 - y0 - 1))
        ov["mark"] = (x, y)
        self.redraw()

    def ov_release(self, e):
        ov = self.ov
        if ov and ov.get("kind") == "color" and ov.get("drag"):
            ov["drag"] = False
            fn = ov.get("pick")
            if fn:
                fn({"color": ov["color"], "final": True})
            self.redraw()

    def ov_wheel(self, direction):
        ov = self.ov
        if ov and ov.get("max_scroll", 0) > 0:
            ov["scroll"] = max(0, min(ov["max_scroll"], ov["scroll"] - direction * 84))
            self.redraw()

    def ov_hover(self, x, y):
        h_ = self.ov_hit(x, y)
        return (h_[4], h_[5]) if h_ and h_[4] in ("ovrow", "ovprev", "ovbtn") else None

    def ov_draw(self, W, H, bg_img, fg, muted, fam, fs):
        """キャンバスの一番上に、パネルを描く (座標は画面そのまま)"""
        ov = self.ov
        if not ov:
            self.ov_hits = ()
            return
        c, th = self.c, self.theme()
        dark = self.cfg["theme"] != "light"
        accent = self.cfg["accent"]
        kind, rows = ov["kind"], ov["rows"]
        RH = 42
        pw = min(W - 28, ov.get("width", 300))
        title_h = 46 if ov.get("title") else 10
        if kind in ("menu", "info"):
            full_h = title_h + len(rows) * RH + 12
            ph = min(full_h, H - 28)
            ov["max_scroll"] = max(0, full_h - ph)
            ov["scroll"] = max(0, min(ov["max_scroll"], ov["scroll"]))
        elif kind == "color":
            ph = title_h + 150 + 14 + 40 + 40
        elif kind == "drop":
            ph = title_h + 270
        else:                                   # input
            ph = title_h + 134
        a = ov["anchor"]
        if a[0] == "tr":
            px, py = a[1] - pw, a[2]
        elif a[0] == "above":
            px, py = a[1] - pw / 2, a[2] - ph
        else:
            px, py = (W - pw) / 2, (H - ph) / 2
        px = int(max(14, min(W - pw - 14, px)))
        py = int(max(10, min(H - ph - 10, py)))
        pad = self.OV_PAD
        if bg_img is None:
            from PIL import Image
            bg_img = Image.new("RGB", (W, H), th["bg"])
        key = (px, py, pw, ph, W, H, self.cfg["theme"], id(bg_img))
        if ov["key"] != key:
            ov["key"] = key
            ov["img"] = frost_panel(bg_img, (px, py, px + pw, py + ph), dark, pad=pad)
            ov["photo_full"] = None
        if ov.get("scrim_key") != (W, H, dark):
            from PIL import Image
            ov["scrim_key"] = (W, H, dark)
            ov["scrim"] = self.photo(Image.new("RGBA", (W, H), (0, 0, 0, 96 if dark else 60)))
        c.create_image(0, 0, anchor="nw", image=ov["scrim"])
        p = self._ov_p()
        vis = int(pad + ph * p) if p < 1.0 else ph + 2 * pad
        if p >= 1.0:
            if ov.get("photo_full") is None:
                ov["photo_full"] = self.photo(ov["img"])
            photo = ov["photo_full"]
        else:
            photo = self.photo(ov["img"].crop((0, 0, ov["img"].width, max(1, vis))))
        ov["photo_cur"] = photo                   # 参照を持っておく (消えないように)
        c.create_image(px - pad, py - pad, anchor="nw", image=photo)
        hits = [(px, py, px + pw, py + ph, "ovbody", None)]
        bottom_vis = py - pad + vis                # いま見えている下端
        panel_col = lambda y: _hex(_mix(self._under(bg_img, px + pw // 2, y), (0, 0, 0) if dark else (255, 255, 255), 0.45 if dark else 0.55))
        def rrect(x0, y0, x1, y1, r, color):
            c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline="")
            c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=color, outline="")
            for ox_, oy_ in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
                c.create_oval(ox_, oy_, ox_ + 2 * r, oy_ + 2 * r, fill=color, outline="")
        def fit(text, size, bold, maxw):
            while len(text) > 1 and self.measure(text, size, bold) > maxw:
                text = text[:-2] + "…"
            return text
        def btn(x0, y0, x1, y1, label, key, accent_btn=False):
            hov = self.hover == ("ovbtn", key)
            col = accent if (accent_btn or hov) else _hex(_mix(_unhex(panel_col(y0)), _unhex(accent), 0.0))
            if hov and not accent_btn:
                col = _hex(_mix(_unhex(panel_col(y0)), _unhex(accent), 0.6))
            rrect(x0, y0, x1, y1, 12, col if (accent_btn or hov) else _hex(_mix(_unhex(panel_col(y0)), (255, 255, 255) if dark else (0, 0, 0), 0.10)))
            c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=label, fill="#ffffff" if (accent_btn or hov) else fg, font=(fam, fs, "bold"))
            hits.append((x0, y0, x1, y1, "ovbtn", key))
        if p >= 0.35 or True:
            if ov.get("title") and py + 28 < bottom_vis:
                c.create_text(px + 22, py + 26, anchor="w", text=fit(ov["title"], fs + 1, True, pw - 44), fill=fg, font=(fam, fs + 1, "bold"))
            if kind in ("menu", "info"):
                sc = ov.get("scroll", 0)
                for i, row in enumerate(rows):
                    y0 = py + title_h + i * RH - sc
                    if y0 + RH - 6 > bottom_vis:
                        break
                    if y0 < py + title_h - 6 or y0 + RH > py + ph - 2:
                        continue
                    if row.get("sep"):
                        c.create_line(px + 20, y0 + RH // 2, px + pw - 20, y0 + RH // 2, fill=muted)
                        continue
                    if kind == "info":
                        c.create_text(px + 22, y0 + RH // 2, anchor="w", text=row["label"], fill=muted, font=(fam, fs))
                        draw_text2(c, self.measure, fam, px + pw - 22, y0 + RH // 2, str(row.get("value", "")), fs, False, pw - 150, "e", fg)
                        continue
                    hov = self.hover == ("ovrow", i)
                    if hov:
                        rrect(px + 8, y0 + 2, px + pw - 8, y0 + RH - 2, 10, _hex(_mix(_unhex(panel_col(y0)), _unhex(accent), 0.55)))
                    draw_text2(c, self.measure, fam, px + 24, y0 + RH // 2, row["label"], fs, False, pw - 100, "w", "#ffffff" if hov else fg)
                    right = px + pw - 24
                    if row.get("preview"):
                        c.create_polygon(right - 12, y0 + RH // 2 - 7, right - 12, y0 + RH // 2 + 7, right + 1, y0 + RH // 2,
                                         fill=accent if not hov else "#ffffff", outline="")
                        hits.append((right - 30, y0, px + pw - 8, y0 + RH, "ovprev", i))
                        right -= 34
                    if row.get("check") is not None:
                        if row["check"]:
                            c.create_text(right, y0 + RH // 2, anchor="e", text="✓", fill=accent if not hov else "#ffffff", font=(fam, fs + 2, "bold"))
                    hits.append((px + 8, y0, px + pw - 8, y0 + RH, "ovrow", i))
            elif kind == "color":
                ax0, ay0, ax1, ay1 = px + 20, py + title_h, px + pw - 20, py + title_h + 150
                if ay1 < bottom_vis:
                    if ov.get("pal_key") != (ax1 - ax0, ay1 - ay0):
                        ov["pal_key"] = (ax1 - ax0, ay1 - ay0)
                        ov["pal_photo"] = self.photo(palette_image(ax1 - ax0, ay1 - ay0))
                    c.create_image(ax0, ay0, anchor="nw", image=ov["pal_photo"])
                    c.create_rectangle(ax0, ay0, ax1, ay1, outline=muted)
                    ov["pal_rect"] = (ax0, ay0, ax1, ay1)
                    hits.append((ax0, ay0, ax1, ay1, "ovpal", None))
                    if ov.get("mark"):
                        mx_, my_ = ov["mark"]
                        c.create_oval(mx_ - 7, my_ - 7, mx_ + 7, my_ + 7, outline="#ffffff", width=2)
                        c.create_oval(mx_ - 8, my_ - 8, mx_ + 8, my_ + 8, outline="#000000")
                sy = ay1 + 14
                if sy + 40 < bottom_vis:
                    pres = list(ov.get("presets", []))
                    for extra_ in ("#ffffff", "#000000"):                  # 白と黒も、いつも選べるようにする
                        if extra_ not in [x.lower() for x in pres]:
                            pres.append(extra_)
                    n_ = max(1, len(pres))
                    step = (pw - 40 - 28) / max(1, n_ - 1) if n_ > 1 else 0
                    for k_, col in enumerate(pres):
                        cx_ = ax0 + 14 + k_ * step
                        c.create_oval(cx_ - 13, sy + 6, cx_ + 13, sy + 32, fill=col, outline="#ffffff" if col.lower() == str(ov.get("color", "")).lower() else muted,
                                      width=3 if col.lower() == str(ov.get("color", "")).lower() else 1)
                        hits.append((cx_ - 14, sy + 5, cx_ + 14, sy + 33, "ovsw", col))
                fy = sy + 40
                if fy + 30 < bottom_vis:
                    c.create_oval(ax0, fy + 6, ax0 + 26, fy + 32, fill=ov.get("color", "#ffffff"), outline="#ffffff", width=2)
                    c.create_text(ax0 + 38, fy + 19, anchor="w", text=str(ov.get("color", "")).upper(), fill=fg, font=(fam, fs, "bold"))
                    c.create_text(ax1, fy + 19, anchor="e", text=ov.get("hint", ""), fill=muted, font=(fam, max(8, fs - 1)))
            elif kind == "drop":
                zx0, zy0, zx1, zy1 = px + 20, py + title_h + 4, px + pw - 20, py + title_h + 150
                if zy1 < bottom_vis:
                    c.create_rectangle(zx0, zy0, zx1, zy1, outline=accent, dash=(5, 4), width=2)
                    c.create_text((zx0 + zx1) // 2, (zy0 + zy1) // 2 - 14, text=ov.get("zone", ""), fill=fg, font=(fam, fs + 5, "bold"))
                    c.create_text((zx0 + zx1) // 2, (zy0 + zy1) // 2 + 20, text=ov.get("hint", ""), fill=muted, font=(fam, max(8, fs - 1)))
                if zy1 + 34 < bottom_vis:
                    draw_text2(c, self.measure, fam, px + 22, zy1 + 24, ov.get("msg", ""), fs, False, pw - 44, "w", muted)
                by0 = py + ph - 52
                if by0 + 36 < bottom_vis:
                    half = (pw - 52) // 2
                    btn(px + 20, by0, px + 20 + half, by0 + 36, ov.get("b1", ""), "browse", True)
                    btn(px + 32 + half, by0, px + pw - 20, by0 + 36, ov.get("b2", ""), "folder")
            else:                                                                # input
                if py + title_h + 20 < bottom_vis:
                    draw_text2(c, self.measure, fam, px + 22, py + title_h + 8, ov.get("hint", ""), fs, False, pw - 44, "w", muted)
                if py + title_h + 56 < bottom_vis and self.cb.get("entry"):
                    ent = self.cb["entry"](ov.get("text", ""))
                    c.create_window(px + 22, py + title_h + 28, window=ent, anchor="nw", width=pw - 44, height=32)
                by0 = py + ph - 50
                if by0 + 36 < bottom_vis:
                    half = (pw - 52) // 2
                    btn(px + 20, by0, px + 20 + half, by0 + 36, ov.get("b1", ""), "ok", True)
                    btn(px + 32 + half, by0, px + pw - 20, by0 + 36, ov.get("b2", ""), "cancel")
        self.ov_hits = tuple(hits)

    @staticmethod
    def _under(img, x, y):
        try:
            return img.getpixel((max(0, min(img.width - 1, int(x))), max(0, min(img.height - 1, int(y)))))
        except Exception:
            return (40, 40, 60)


class PlayerView(OverlayMixin):
    """NC Beats タブの描画と操作。Canvas に、背景・ジャケット・文字・ボタンを自前で描く。
    cb: toggle_on() / step_scale(d) / meter(xroot,yroot) / sounds() / conn() / set_vol(v) / set_tim(v)"""

    def __init__(self, canvas, cfg, theme_fn, family_fn, measure_fn, photo_fn, cb, tr_fn):
        self.c, self.cfg, self.theme, self.family = canvas, cfg, theme_fn, family_fn
        self.measure, self.photo, self.cb, self.tr = measure_fn, photo_fn, cb, tr_fn
        self.cover_src, self.cover_id = None, 0
        self.st = {"head": "", "title": "", "sub": "", "dot": "#888888", "scale": 1.0, "meter": "AUTO"}
        self.hits, self.hover, self.drag = [], None, None
        self.bg_key, self.bg_img, self.bg_photo = None, None, None
        self.radius = 0                      # 枠なしフルスクリーンのとき、ウィンドウの角を丸める半径 (0=丸めない)
        self.built_cid, self.slide, self.slide_on, self._rect = None, None, True, (0, 62, 0, 0)
        self._pending = None
        canvas.bind("<Configure>", lambda e: self.schedule())
        canvas.bind("<Button-1>", self.on_down)
        canvas.bind("<B1-Motion>", self.on_move)
        canvas.bind("<ButtonRelease-1>", self.on_up)
        canvas.bind("<Motion>", self.on_hover)

    # ---------- 外から呼ぶ ----------
    def set_cover(self, src, cid):
        self.cover_src, self.cover_id = src, cid
        self.redraw()

    def set_state(self, **kw):
        if any(self.st.get(k) != v for k, v in kw.items()):
            self.st.update(kw)
            self.redraw()

    def schedule(self):
        if self._pending is None:
            self._pending = self.c.after(60, self._run)

    def _run(self):
        self._pending = None
        self.redraw()

    def _rounded(self, im):
        return round_corners(im, self.radius)

    # ---------- スライドの動き ----------
    def _progress(self):
        t = (time.time() - self.slide["t0"]) / 0.40
        return 1 - (1 - min(1.0, max(0.0, t))) ** 3          # ゆっくり止まる動き

    def _place(self, p):
        sl = self.slide
        if not sl or not sl.get("ids"):
            return
        x0, y0 = self._rect[0] + self.ox, self._rect[1] + self.oy
        D = self._w
        try:
            self.c.coords(sl["ids"][0], x0 - p * D, y0)
            self.c.coords(sl["ids"][1], x0 + (1 - p) * D, y0)
        except Exception:
            pass

    def _slide_tick(self):
        if self.slide is None:
            return
        if time.time() - self.slide["t0"] >= 0.40:
            self.slide = None
            self.redraw()
            return
        self._place(self._progress())
        self.c.after(16, self._slide_tick)

    # ---------- 描画 ----------
    def geometry(self):
        W = max(int(self.c.winfo_width()), 320)
        H = max(int(self.c.winfo_height()), 420)
        # 縦長(スマホ比率)の1列に内容をまとめ、広い画面 (フルスクリーン) では中央に置く
        w = min(W, max(340, int(H * 0.6)))
        pad = 28
        C = max(120, min(w - 2 * pad, H - 345))
        x0 = (w - C) // 2
        self.ox = (W - w) // 2
        self.oy = max(0, (H - (62 + C + 278)) // 2)
        return W, H, w, pad, (x0, 62, x0 + C, 62 + C)

    def redraw(self):
        th = self.theme()
        W, h, w, pad, rect = self.geometry()
        ox, oy = self.ox, self.oy
        accent = self.cfg["accent"]
        key = (W, h, rect, ox, oy, self.cover_id, self.cfg["theme"], accent, self.radius)
        if key != self.bg_key:
            self.bg_key = key
            prev_img, prev_cid = self.bg_img, self.built_cid
            try:
                full = (rect[0] + ox, rect[1] + oy, rect[2] + ox, rect[3] + oy)
                self.bg_img = build_player_bg(W, h, full, self.cover_src, accent, self.cfg["theme"])
                self.bg_photo = self.photo(self._rounded(self.bg_img))
                self.built_cid = self.cover_id
                if (self.slide_on and prev_img is not None and prev_cid is not None and prev_cid != self.cover_id
                        and prev_img.size == self.bg_img.size and hasattr(self.c, "coords")):
                    # 譜面が変わった: 古いジャケットが左へ出ていき、新しいジャケットが右から入ってくる
                    def tile(im):
                        t = im.crop(full).convert("RGBA")
                        t.putalpha(_round_mask((full[2] - full[0], full[3] - full[1]), 10))
                        return self.photo(t)
                    nc = self._rounded(build_player_bg(W, h, full, self.cover_src, accent, self.cfg["theme"], draw_cover=False))
                    self.slide = {"t0": time.time(), "old": tile(prev_img), "new": tile(self.bg_img),
                                  "nc": self.photo(nc), "ids": None}
                    self.c.after(16, self._slide_tick)
            except Exception:
                self.bg_img, self.bg_photo = None, None
        c, st, fam, fs = self.c, self.st, self.family(), int(self.cfg["font_size"])
        c.delete("all")
        bg_item = None
        sl = self.slide
        if sl is not None:
            bg_item = c.create_image(0, 0, anchor="nw", image=sl["nc"])
            sl["ids"] = (c.create_image(rect[0], rect[1], anchor="nw", image=sl["old"]),
                         c.create_image(rect[0], rect[1], anchor="nw", image=sl["new"]))
            self._rect, self._w = rect, w
        elif self.bg_photo is not None:
            bg_item = c.create_image(0, 0, anchor="nw", image=self.bg_photo)
        else:
            c.create_rectangle(0, 0, W, h, fill=th["bg"], outline="")
        fg, muted = th["fg"], PLAYER_MUTED.get(self.cfg["theme"], PLAYER_MUTED["dark"])
        dark = self.cfg["theme"] != "light"
        self.hits = []

        def under(x, y):                      # 背景の色 (ボタンやトラックの色づくりに使う)
            if self.bg_img is not None:
                try:
                    return self.bg_img.getpixel((max(0, min(W - 1, int(x + ox))), max(0, min(h - 1, int(y + oy)))))
                except Exception:
                    pass
            return _unhex(th["bg"])
        def panel(x, y):
            return _hex(_mix(under(x, y), (0, 0, 0) if dark else (255, 255, 255), 0.38 if dark else 0.55))
        def rrect(x0, y0, x1, y1, r, color):
            c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline="")
            c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=color, outline="")
            for ox, oy in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
                c.create_oval(ox, oy, ox + 2 * r, oy + 2 * r, fill=color, outline="")
        def fit(text, size, bold, maxw):
            while len(text) > 1 and self.measure(text, size, bold) > maxw:
                text = text[:-2] + "…"
            return text
        def hot(name):
            return self.hover == name

        # --- 上部 ---
        if self.cover_src is None and sl is None:
            c.create_text((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2, text="♪", fill=muted, font=(fam, 56))
        c.create_text(w // 2, 18, text=self.tr("player.from"), fill=muted, font=(fam, max(8, fs - 1)))
        c.create_text(w // 2, 38, text=st["head"], fill=fg, font=(fam, fs, "bold"))
        # 左上: フルスクリーン  /  右上: メニュー (Skin Hitsound / Setting / 接続状態) + 接続の色
        fx, fy = pad + 8, 28
        fc = self.cfg["accent"] if hot("full") else fg
        for sx, sy_ in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            c.create_line(fx + sx * 9, fy + sy_ * 4, fx + sx * 9, fy + sy_ * 9, fill=fc, width=2)
            c.create_line(fx + sx * 9, fy + sy_ * 9, fx + sx * 4, fy + sy_ * 9, fill=fc, width=2)
        self.hits.append((fx - 16, fy - 16, fx + 16, fy + 16, "full"))
        mx1 = w - pad; mx0 = mx1 - 44
        rrect(mx0, fy - 14, mx1, fy + 14, 13, _hex(_mix(_unhex(panel(mx0, fy)), _unhex(self.cfg["accent"]), 0.85)) if hot("menu") else panel(mx0, fy))
        c.create_text((mx0 + mx1) // 2, fy, text="•••", fill=fg, font=(fam, fs, "bold"))
        c.create_oval(mx0 - 20, fy - 5, mx0 - 10, fy + 5, fill=st["dot"], outline="")
        self.hits.append((mx0 - 24, fy - 16, mx1 + 4, fy + 16, "menu"))

        # --- タイトル行 ---
        ty = rect[3] + 36
        mqs = []                                                       # 長い題名・アーティスト名は、右から左へ流す
        self._mq_line(c, st["title"], pad, ty, fs + 5, True, w - 2 * pad, fg, fam, mqs, ox, oy, W, h, sl is None)
        self._mq_line(c, st["sub"], pad, ty + 26, fs, False, w - 2 * pad, muted, fam, mqs, ox, oy, W, h, sl is None)

        # --- スライダー (音量 / タイミング) ---
        def slider(y, label, name, lo, hi, val, fmt):
            c.create_text(pad, y, anchor="w", text=label, fill=fg, font=(fam, fs))
            c.create_text(w - pad, y, anchor="e", text=fmt(val), fill=fg, font=(fam, fs))
            x0, x1 = pad + 84, w - pad - 66
            frac = (val - lo) / (hi - lo)
            kx = x0 + (x1 - x0) * max(0.0, min(1.0, frac))
            rrect(x0, y - 2, x1, y + 2, 2, panel(x0, y))
            rrect(x0, y - 2, max(x0 + 4, kx), y + 2, 2, self.cfg["accent"])
            r = 8 if (hot(name) or (self.drag and self.drag[0] == name)) else 6
            c.create_oval(kx - r, y - r, kx + r, y + r, fill=fg, outline="")
            self.hits.append((x0 - 8, y - 14, x1 + 8, y + 14, name, x0, x1, lo, hi))
        sy = ty + 66
        slider(sy, self.tr("lbl.volume"), "vol", 0.0, 1.0, float(self.cfg["volume"]), lambda v: f"{int(v * 100)}%")
        slider(sy + 34, self.tr("lbl.timing"), "tim", -150.0, 150.0, float(self.cfg["offset_ms"]), lambda v: f"{int(v)} ms")

        # --- 操作ボタン ---
        cy = sy + 34 + 66
        xs = [pad + (w - 2 * pad) * (i + 0.5) / 5 for i in range(5)]
        def sidebtn(x, name, text):
            x0, x1, y0, y1 = x - 31, x + 31, cy - 19, cy + 19
            col = _hex(_mix(_unhex(panel(x, cy)), _unhex(self.cfg["accent"]), 0.85)) if hot(name) else panel(x, cy)
            rrect(x0, y0, x1, y1, 10, col)
            c.create_text(x, cy, text=text, fill=fg, font=(fam, fs + 2, "bold"))
            self.hits.append((x0, y0, x1, y1, name))
        sidebtn(xs[0], "meter", st["meter"])
        sidebtn(xs[4], "sounds", "♪")
        icol = lambda n: self.cfg["accent"] if hot(n) else fg
        m = xs[1]                                                    # ⏮ (倍率を下げる)
        c.create_rectangle(m - 13, cy - 11, m - 10, cy + 11, fill=icol("prev"), outline="")
        c.create_polygon(m + 12, cy - 12, m + 12, cy + 12, m - 8, cy, fill=icol("prev"), outline="")
        self.hits.append((m - 24, cy - 24, m + 24, cy + 24, "prev"))
        m = xs[3]                                                    # ⏭ (倍率を上げる)
        c.create_rectangle(m + 10, cy - 11, m + 13, cy + 11, fill=icol("next"), outline="")
        c.create_polygon(m - 12, cy - 12, m - 12, cy + 12, m + 8, cy, fill=icol("next"), outline="")
        self.hits.append((m - 24, cy - 24, m + 24, cy + 24, "next"))
        m = xs[2]                                                    # 再生/一時停止 = ON/OFF
        R = 34
        c.create_oval(m - R, cy - R, m + R, cy + R, fill=self.cfg["accent"] if hot("play") else fg, outline="")
        glyph = _hex(_unhex(th["bg"]))
        if self.cfg.get("beats_on", True):
            c.create_rectangle(m - 10, cy - 12, m - 3, cy + 12, fill=glyph, outline="")
            c.create_rectangle(m + 3, cy - 12, m + 10, cy + 12, fill=glyph, outline="")
        else:
            c.create_polygon(m - 7, cy - 13, m - 7, cy + 13, m + 13, cy, fill=glyph, outline="")
        self.hits.append((m - R, cy - R, m + R, cy + R, "play"))
        # 倍率の値だけ小さく表示 (説明の文字は入れない)
        c.create_text(xs[2], cy + 52, text=f"x{float(st['scale']):g}", fill=muted, font=(fam, fs))
        # 背景以外を、中央寄せの位置へ動かす
        for it in c.find_all():
            if it != bg_item:
                c.move(it, ox, oy)
        self.hits = [(h_[0] + ox, h_[1] + oy, h_[2] + ox, h_[3] + oy) + (h_[4],) +
                     ((h_[5] + ox, h_[6] + ox) + tuple(h_[7:]) if len(h_) > 5 else ())
                     for h_ in self.hits]
        if sl is not None:
            self._place(self._progress())
        self._mq_start(mqs, ox, oy)
        self.ov_draw(W, h, self.bg_img, fg, muted, fam, fs)

    # ---------- 長い文字を右から左へ流す ----------
    MQ_SPEED, MQ_GAP, MQ_WAIT = 45.0, 60, 1.5

    def _mq_line(self, c, text, x, y, size, bold, maxw, fill, fam, mqs, ox, oy, W, H, can):
        """収まるならそのまま描く。収まらないときは、左右を背景でかくして流す (キャンバスは切り取れないため)"""
        font = (fam, size, "bold") if bold else (fam, size)
        tw = self.measure(text, size, bold)
        if tw <= maxw:
            c.create_text(x, y, anchor="w", text=text, fill=fill, font=font)
            return
        if not can or self.bg_img is None or not hasattr(c, "coords"):
            while len(text) > 1 and self.measure(text, size, bold) > maxw:
                text = text[:-2] + "…"
            c.create_text(x, y, anchor="w", text=text, fill=fill, font=font)
            return
        ids = [c.create_text(x, y, anchor="w", text=text, fill=fill, font=font),
               c.create_text(x + tw + self.MQ_GAP, y, anchor="w", text=text, fill=fill, font=font)]
        lh = int(size * 1.8)
        y0, y1 = int(y - lh / 2) + oy, int(y + lh / 2) + oy
        bw = self.bg_img.width
        for a, b in ((0, int(x) + ox), (int(x + maxw) + ox, bw)):          # 左右のはみ出しをかくす
            if b > a:
                try:
                    im = self.photo(self.bg_img.crop((a, y0, b, y1)).convert("RGB"))
                    self._mq_keep = getattr(self, "_mq_keep", [])[-8:] + [im]
                    c.create_image(a - ox, y0 - oy, anchor="nw", image=im)
                except Exception:
                    pass
        mqs.append((ids, x, y, tw))

    def _mq_start(self, mqs, ox, oy):
        job = getattr(self, "_mq_job", None)
        if job is not None:
            try:
                self.c.after_cancel(job)
            except Exception:
                pass
            self._mq_job = None
        key = (self.st["title"], self.st["sub"])
        if key != getattr(self, "_mq_key", None):
            self._mq_key, self._mq_t0 = key, time.time()
        self._mq = [(ids, x + ox, y + oy, tw) for ids, x, y, tw in mqs]
        if self._mq:
            self._mq_tick()

    def _mq_tick(self):
        mq = getattr(self, "_mq", None)
        if not mq:
            return
        t = time.time() - self._mq_t0
        for ids, x, y, tw in mq:
            per = (tw + self.MQ_GAP) / self.MQ_SPEED
            ph = t % (per + self.MQ_WAIT)
            off = 0.0 if ph < self.MQ_WAIT else (ph - self.MQ_WAIT) * self.MQ_SPEED
            try:
                self.c.coords(ids[0], x - off, y)
                self.c.coords(ids[1], x - off + tw + self.MQ_GAP, y)
            except Exception:
                self._mq = None
                return
        self._mq_job = self.c.after(33, self._mq_tick)

    # ---------- 操作 ----------
    def hit(self, x, y):
        for h_ in reversed(self.hits):
            if h_[0] <= x <= h_[2] and h_[1] <= y <= h_[3]:
                return h_
        return None

    def _slide(self, h_, x):
        name, x0, x1, lo, hi = h_[4], h_[5], h_[6], h_[7], h_[8]
        v = lo + (hi - lo) * max(0.0, min(1.0, (x - x0) / (x1 - x0)))
        if name == "vol":
            self.cb["set_vol"](v)
        else:
            self.cb["set_tim"](int(round(v)))
        self.redraw()

    def on_down(self, e):
        if self.ov:
            return self.ov_click(e)
        h_ = self.hit(e.x, e.y)
        self.bgdrag = not h_
        if not h_:
            if "bg_down" in self.cb: self.cb["bg_down"](e)   # 何もない所をつかんでウィンドウを動かす (枠なしの時)
            return
        name = h_[4]
        if name in ("vol", "tim"):
            self.drag = (name, h_); self._slide(h_, e.x)
        elif name == "play": self.cb["toggle_on"]()
        elif name == "prev": self.cb["step_scale"](-1)
        elif name == "next": self.cb["step_scale"](1)
        elif name == "meter": self.cb["meter"](self, ("above", (h_[0] + h_[2]) / 2, h_[1] - 8))
        elif name == "sounds": self.cb["sounds"](self, ("above", h_[2] + 6, h_[1] - 8))
        elif name == "menu": self.cb["menu"](self, ("tr", h_[2], h_[3] + 4))
        elif name == "full": self.cb["full"]()

    def on_move(self, e):
        if self.drag:
            self._slide(self.drag[1], e.x)
        elif getattr(self, "bgdrag", False) and "bg_move" in self.cb:
            self.cb["bg_move"](e)

    def on_up(self, e):
        if self.drag:
            self.drag = None
            self.redraw()

    def on_hover(self, e):
        if self.ov:
            nh = self.ov_hover(e.x, e.y)
            if nh != self.hover:
                self.hover = nh
                self.redraw()
            return
        h_ = self.hit(e.x, e.y)
        name = h_[4] if h_ else None
        if name != self.hover:
            self.hover = name
            try:
                self.c.configure(cursor="hand2" if name else "")
            except Exception:
                pass
            self.redraw()



class SettingView(OverlayMixin):
    """Setting 画面。メトロノーム画面と同じ背景の上に、半透明のパネルで設定の項目を並べる。
    行の種類 (rows の要素 dict): head(見出し) / menu(選ぶと一覧が開く) / color(色の選択) / toggle(有効・無効) / slider / button
    cb: back() menu(view,anchor) bg_down(e) bg_move(e)"""
    ROWH, HEADH = 48, 40

    def __init__(self, canvas, cfg, theme_fn, family_fn, measure_fn, photo_fn, cb, tr_fn):
        self.c, self.cfg, self.theme, self.family = canvas, cfg, theme_fn, family_fn
        self.measure, self.photo, self.cb, self.tr = measure_fn, photo_fn, cb, tr_fn
        self.cover_src, self.cover_id = None, 0
        self.rows, self.hits, self.hover, self.drag = [], [], None, None
        self.bg_key, self.bg_img, self.bg_photo = None, None, None
        self.radius, self.scroll, self._max_scroll, self._ox = 0, 0, 0, 0
        self.tog, self._pending, self._glass = {}, None, (None, None)
        canvas.bind("<Configure>", lambda e: self.schedule())
        canvas.bind("<Button-1>", self.on_down)
        canvas.bind("<B1-Motion>", self.on_move)
        canvas.bind("<ButtonRelease-1>", self.on_up)
        canvas.bind("<Motion>", self.on_hover)
        canvas.bind("<MouseWheel>", self.on_wheel)
        canvas.bind("<Button-4>", lambda e: self.on_wheel(e, 1))
        canvas.bind("<Button-5>", lambda e: self.on_wheel(e, -1))

    def set_rows(self, rows):
        self.rows = rows
        self.redraw()

    def schedule(self):
        if self._pending is None:
            self._pending = self.c.after(60, self._run)

    def _run(self):
        self._pending = None
        self.redraw()

    def _tp(self, i):
        t = self.tog.get(i)
        if not t:
            return None
        k = max(0.0, min(1.0, (time.time() - t[0]) / 0.16))
        return (1 - (1 - k) ** 3) if t[1] else 1 - (1 - (1 - k) ** 3), k

    def _ttick(self):
        if any((time.time() - t[0]) < 0.16 for t in self.tog.values()):
            self.redraw()
            self.c.after(16, self._ttick)
        else:
            self.tog.clear()
            self.redraw()

    def redraw(self):
        th = self.theme()
        W = max(int(self.c.winfo_width()), 320)
        h = max(int(self.c.winfo_height()), 420)
        w = min(W, max(340, int(h * 0.6)))
        ox = (W - w) // 2
        self._ox = ox
        pad = 28
        accent = self.cfg["accent"]
        dark = self.cfg["theme"] != "light"
        C = max(120, min(w - 2 * pad, h - 345))
        oy = max(0, (h - (62 + C + 278)) // 2)
        full = ((w - C) // 2 + ox, 62 + oy, (w - C) // 2 + C + ox, 62 + C + oy)
        key = (W, h, ox, self.cover_id, self.cfg["theme"], accent, self.radius)
        if key != self.bg_key:
            self.bg_key = key
            try:
                self.bg_img = build_player_bg(W, h, full, self.cover_src, accent, self.cfg["theme"], draw_cover=False, halo_only=True)
                self.bg_photo = self.photo(round_corners(self.bg_img, self.radius))
            except Exception:
                self.bg_img, self.bg_photo = None, None
            self._glass = (None, None)
        c, fam, fs = self.c, self.family(), int(self.cfg["font_size"])
        c.delete("all")
        if self.bg_photo is not None:
            bg_item = c.create_image(0, 0, anchor="nw", image=self.bg_photo)
        else:
            bg_item = c.create_rectangle(0, 0, W, h, fill=th["bg"], outline="")
        fg, muted = th["fg"], PLAYER_MUTED.get(self.cfg["theme"], PLAYER_MUTED["dark"])
        self.hits = []
        mixdir = (0, 0, 0) if dark else (255, 255, 255)

        def under(x, y):
            return self._under(self.bg_img, x + ox, y) if self.bg_img is not None else _unhex(th["bg"])
        def gcol(y):
            return _hex(_mix(under(w // 2, y), mixdir, 0.45 if dark else 0.55))
        def rrect(x0, y0, x1, y1, r, color):
            r = max(1, min(r, (x1 - x0) // 2, (y1 - y0) // 2))
            c.create_rectangle(x0 + r, y0, x1 - r, y1, fill=color, outline="")
            c.create_rectangle(x0, y0 + r, x1, y1 - r, fill=color, outline="")
            for ox_, oy_ in ((x0, y0), (x1 - 2 * r, y0), (x0, y1 - 2 * r), (x1 - 2 * r, y1 - 2 * r)):
                c.create_oval(ox_, oy_, ox_ + 2 * r, oy_ + 2 * r, fill=color, outline="")
        def fit(text, size, bold, maxw):
            while len(text) > 1 and self.measure(text, size, bold) > maxw:
                text = text[:-2] + "…"
            return text

        # --- 上部: 戻る / タイトル / メニュー ---
        fc = accent if self.hover == "back" else fg
        c.create_line(pad + 14, 19, pad + 5, 28, pad + 14, 37, fill=fc, width=2)
        self.hits.append((pad - 8, 10, pad + 32, 46, "back", None))
        c.create_text(w // 2, 28, text=self.tr("tab.set"), fill=fg, font=(fam, fs + 1, "bold"))
        mx1 = w - pad; mx0 = mx1 - 44
        rrect(mx0, 14, mx1, 42, 13, _hex(_mix(_mix(under(mx0, 28), mixdir, 0.38 if dark else 0.55), _unhex(accent), 0.85)) if self.hover == "menu"
              else _hex(_mix(under(mx0, 28), mixdir, 0.38 if dark else 0.55)))
        c.create_text((mx0 + mx1) // 2, 28, text="•••", fill=fg, font=(fam, fs, "bold"))
        self.hits.append((mx0 - 6, 10, mx1 + 4, 46, "menu", None))

        # --- 項目のパネル (すりガラス風) ---
        lx0, lx1 = pad, w - pad
        ly0, ly1 = 66, h - 22
        if self.bg_img is not None:
            gk = (W, h, ox, ly0, ly1, self.cfg["theme"], id(self.bg_img))
            if self._glass[0] != gk:
                self._glass = (gk, self.photo(frost_panel(self.bg_img, (lx0 + ox, ly0, lx1 + ox, ly1), dark, radius=20, pad=24)))
            c.create_image(lx0 - 24, ly0 - 24, anchor="nw", image=self._glass[1])
        else:
            rrect(lx0, ly0, lx1, ly1, 20, th["field"])
        y = ly0 + 8 - self.scroll
        for i, row in enumerate(self.rows):
            kind = row["kind"]
            rh = self.HEADH if kind == "head" else self.ROWH
            top = y
            y += rh
            if top < ly0 + 2 or top + rh > ly1 - 2:
                continue
            mid = top + rh // 2
            if kind == "head":
                c.create_text(lx0 + 22, mid + 6, anchor="w", text=row["label"], fill=accent, font=(fam, max(8, fs - 1), "bold"))
                continue
            hv = self.hover == ("row", i)
            if hv and kind != "slider":
                rrect(lx0 + 8, top + 3, lx1 - 8, top + rh - 3, 12, _hex(_mix(_unhex(gcol(mid)), _unhex(accent), 0.5)))
            tcol = "#ffffff" if (hv and kind != "slider") else fg
            vtxt = str(row.get("value", "")) if kind == "menu" else ""
            side = 70 + (min(self.measure(vtxt, fs, False), int((lx1 - lx0) * 0.36)) + 24 if kind == "menu" else 0)
            lab_w = max(80, (lx1 - lx0) - 24 - side)
            nl = draw_text2(c, self.measure, fam, lx0 + 24, mid, row["label"], fs, False, lab_w, "w", tcol)
            rx = lx1 - 24
            lw = min(self.measure(row["label"], fs, False), lab_w) if nl == 1 else lab_w
            if kind in ("menu", "color", "button"):
                c.create_line(rx - 6, mid - 5, rx, mid, rx - 6, mid + 5, fill=tcol if hv else muted, width=2)
                rx -= 18
                if kind == "menu":
                    draw_text2(c, self.measure, fam, rx, mid, str(row.get("value", "")), fs, False, max(40, lx1 - lx0 - lw - 100), "e",
                               "#ffffff" if hv else muted)
                elif kind == "color":
                    c.create_oval(rx - 22, mid - 11, rx, mid + 11, fill=row.get("value", "#ffffff"), outline="#ffffff", width=2)
                self.hits.append((lx0 + 8, top + 3, lx1 - 8, top + rh - 3, "row", i))
            elif kind == "toggle":
                tw, th_ = 46, 26
                tx1 = lx1 - 24; tx0 = tx1 - tw
                tp = self._tp(i)
                p = tp[0] if tp else (1.0 if row.get("on") else 0.0)
                off_col = _hex(_mix(_unhex(gcol(mid)), (190, 190, 200), 0.62))
                track = _hex(_mix(_unhex(off_col), _unhex(accent), p))
                rrect(tx0, mid - th_ // 2, tx1, mid + th_ // 2, th_ // 2, track)
                kx = tx0 + 13 + p * (tw - 26)
                c.create_oval(kx - 10, mid - 10, kx + 10, mid + 10, fill="#1c1c22", outline="")
                self.hits.append((lx0 + 8, top + 3, lx1 - 8, top + rh - 3, "row", i))
            elif kind == "slider":
                lo, hi, val = row["lo"], row["hi"], row["val"]
                vtxt = row["fmt"](val)
                c.create_text(rx, mid, anchor="e", text=vtxt, fill=fg, font=(fam, fs))
                tx0 = lx0 + 24 + max(lw, 90) + 14
                tx1 = rx - max(self.measure(vtxt, fs, False), 50) - 14
                kx = tx0 + (tx1 - tx0) * max(0.0, min(1.0, (val - lo) / float(hi - lo)))
                rrect(tx0, mid - 2, tx1, mid + 2, 2, _hex(_mix(_unhex(gcol(mid)), (255, 255, 255) if dark else (0, 0, 0), 0.25)))
                rrect(tx0, mid - 2, max(tx0 + 4, kx), mid + 2, 2, accent)
                r_ = 8 if (self.hover == ("row", i) or (self.drag and self.drag[1] == i)) else 6
                c.create_oval(kx - r_, mid - r_, kx + r_, mid + r_, fill=fg, outline="")
                self.hits.append((tx0 - 10, top + 6, tx1 + 10, top + rh - 6, "sl", (i, tx0, tx1)))
        total = (y + self.scroll) - ly0 + 8
        self._max_scroll = max(0, total - (ly1 - ly0))
        self.scroll = max(0, min(self._max_scroll, self.scroll))
        for it in c.find_all():
            if it != bg_item:
                c.move(it, ox, 0)
        self.hits = [(h_[0] + ox, h_[1], h_[2] + ox, h_[3], h_[4], h_[5]) for h_ in self.hits]
        self.ov_draw(W, h, self.bg_img, fg, muted, fam, fs)

    # ---------- 操作 ----------
    def hit(self, x, y):
        for h_ in reversed(self.hits):
            if h_[0] <= x <= h_[2] and h_[1] <= y <= h_[3]:
                return h_
        return None

    def _set_slider(self, x):
        _, i, tx0, tx1 = self.drag
        row = self.rows[i]
        lx = x - self._ox
        v = row["lo"] + (row["hi"] - row["lo"]) * max(0.0, min(1.0, (lx - tx0) / float(tx1 - tx0)))
        row["val"] = int(round(v)) if row.get("int") else v
        if row.get("on"):
            row["on"](row["val"])
        self.redraw()

    def on_down(self, e):
        if self.ov:
            return self.ov_click(e)
        h_ = self.hit(e.x, e.y)
        self.bgdrag = not h_
        if not h_:
            if "bg_down" in self.cb: self.cb["bg_down"](e)
            return
        name, p = h_[4], h_[5]
        if name == "back": self.cb["back"]()
        elif name == "menu": self.cb["menu"](self, ("tr", h_[2], h_[3] + 4))
        elif name == "sl":
            self.drag = ("sl", p[0], p[1], p[2]); self._set_slider(e.x)
        elif name == "row":
            row = self.rows[p]
            if row["kind"] == "toggle":
                row["on"] = not row.get("on")
                self.tog[p] = (time.time(), row["on"])
                row["set"](row["on"])
                self._ttick()
            else:
                row["run"](self, ("tr", h_[2], h_[3] + 4))

    def on_move(self, e):
        if self.ov:
            return self.ov_move(e)
        if self.drag:
            self._set_slider(e.x)
        elif getattr(self, "bgdrag", False) and "bg_move" in self.cb:
            self.cb["bg_move"](e)

    def on_up(self, e):
        if self.ov:
            return self.ov_release(e)
        if self.drag:
            row = self.rows[self.drag[1]]
            self.drag = None
            if row.get("done"):
                row["done"]()
            self.redraw()

    def on_wheel(self, e, direction=None):
        d = direction if direction is not None else (1 if getattr(e, "delta", 0) > 0 else -1)
        if self.ov:
            return self.ov_wheel(d)
        self.scroll = max(0, min(self._max_scroll, self.scroll - d * 48))
        self.redraw()

    def on_hover(self, e):
        if self.ov:
            nh = self.ov_hover(e.x, e.y)
            if nh != self.hover:
                self.hover = nh
                self.redraw()
            return
        h_ = self.hit(e.x, e.y)
        if h_ is None:
            nm = None
        elif h_[4] == "row":
            nm = ("row", h_[5])
        elif h_[4] == "sl":
            nm = ("row", h_[5][0])
        else:
            nm = h_[4]
        if nm != self.hover:
            self.hover = nm
            try:
                self.c.configure(cursor="hand2" if nm else "")
            except Exception:
                pass
            self.redraw()


def clean_cover(img):
    """譜面画像を、表示用のRGB画像にする。
    - 透明な部分のある画像 (PNGなど) は、暗い色の上に重ねる (単に透明を捨てると、黒や白の抜けになる)
    - 「透明にする色」(枠なしの角の丸み用) と同じ色が、画像の中に出ないようにする。暗い画像で、その部分が透けて見えるのを防ぐ"""
    from PIL import Image
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (24, 24, 28))
        base.paste(rgba, (0, 0), rgba.split()[3])
        img = base
    else:
        img = img.convert("RGB")
    return img.point(lambda v: max(v, 4))


def load_cover_src(data):
    """背景画像のバイト列 -> 表示用のPIL画像 (長辺600pxまで縮小)。読めなければ None"""
    try:
        import io
        from PIL import Image
        img = clean_cover(Image.open(io.BytesIO(data)))
        img.thumbnail((600, 600))
        return img
    except Exception:
        return None


def make_cover(data, size):
    """背景画像のバイト列 -> 正方形に中央切り抜きしたPIL画像。Pillowが無い/読めない場合は None"""
    try:
        import io
        from PIL import Image, ImageOps
        img = clean_cover(Image.open(io.BytesIO(data)))
        return ImageOps.fit(img, (size, size), getattr(Image, "Resampling", Image).LANCZOS)
    except Exception:
        return None


def run_gui(engine, bank, cfg):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    import tkinter.font as tkfont
    import webbrowser
    AACanvas = make_aa_canvas_class(tk, lambda: cfg.get("smooth", True))     # 円・多角形・線をなめらかに描く Canvas

    root = tk.Tk()
    root.title(APP_TITLE)
    try:                                              # ウィンドウのアイコン (exe版は同梱のicon.ico)
        ico = os.path.join(getattr(sys, "_MEIPASS", HERE), "icon.ico")
        if sys.platform == "win32" and os.path.isfile(ico):
            root.iconbitmap(ico)
    except Exception:
        pass
    size0 = str(cfg.get("window_size", ""))
    try:
        ww, hh = [int(v) for v in size0.split("x")]
        root.geometry(f"{max(340, min(ww, 3000))}x{max(560, min(hh, 3000))}")      # 前回の大きさ
    except Exception:
        root.geometry("420x748")      # スマホのような縦長
    root.minsize(340, 560)
    root.attributes("-topmost", bool(cfg["always_on_top"] or cfg.get("hide_in_play")))
    theme_hooks = []   # テーマ変更時に呼ぶ関数 (ttk以外の部品用)
    ui = {}            # 作り直しても参照できるように、画面の部品や関数をここに入れる
    load_lang_files()
    set_language(cfg["language"])

    # ---------- 見た目 (フォント / テーマ) ----------
    def apply_fonts():
        fams = set(tkfont.families(root))
        fam = cfg["font_family"] if cfg["font_family"] in fams else tkfont.nametofont("TkDefaultFont").actual("family")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=fam, size=int(cfg["font_size"]))

    def apply_theme():
        th = THEMES.get(cfg["theme"], THEMES["dark"])
        acc = cfg["accent"]
        st = ttk.Style(root)
        st.theme_use("clam")
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        root.configure(bg=th["bg"])
        st.configure(".", background=th["bg"], foreground=th["fg"], fieldbackground=th["field"],
                     bordercolor=th["panel"], lightcolor=th["panel"], darkcolor=th["panel"],
                     troughcolor=th["panel"], focuscolor=th["bg"])
        st.configure("TFrame", background=th["bg"])
        st.configure("TLabel", background=th["bg"], foreground=th["fg"])
        st.configure("Muted.TLabel", background=th["bg"], foreground=th["muted"])
        st.configure("Head.TLabel", background=th["bg"], foreground=acc, font=(fam, int(cfg["font_size"]) + 2, "bold"))
        st.configure("Title.TLabel", background=th["bg"], foreground=th["fg"], font=(fam, int(cfg["font_size"]) + 5, "bold"))
        st.configure("Bold.TLabel", background=th["bg"], foreground=th["fg"], font=(fam, int(cfg["font_size"]), "bold"))
        st.configure("Side.TButton", background=th["panel"], foreground=th["fg"], padding=(8, 8), borderwidth=0,
                     font=(fam, int(cfg["font_size"]) + 3, "bold"))
        st.map("Side.TButton", background=[("active", acc)], foreground=[("active", "#ffffff")])
        st.configure("TSeparator", background=th["panel"])
        st.configure("TButton", background=th["panel"], foreground=th["fg"], padding=(12, 6), borderwidth=0)
        st.map("TButton", background=[("active", acc), ("pressed", acc)], foreground=[("active", "#ffffff")])
        st.configure("Accent.TButton", background=acc, foreground="#ffffff", padding=(12, 6), borderwidth=0)
        st.map("Accent.TButton", background=[("active", th["fg"])], foreground=[("active", th["bg"])])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=th["bg"], foreground=th["fg"],
                         indicatorbackground=th["field"], indicatorforeground=th["fg"])
            st.map(w, background=[("active", th["bg"])],
                   indicatorbackground=[("selected", acc), ("!selected", th["field"])],
                   indicatorcolor=[("selected", acc), ("!selected", th["field"])])
        st.configure("TNotebook", background=th["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=th["panel"], foreground=th["muted"], padding=(20, 9), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", acc)], foreground=[("selected", "#ffffff")])
        st.configure("Horizontal.TScale", background=th["bg"], troughcolor=th["panel"])
        st.configure("TCombobox", fieldbackground=th["field"], background=th["panel"], foreground=th["fg"],
                     arrowcolor=th["fg"], bordercolor=th["panel"])
        st.map("TCombobox", fieldbackground=[("readonly", th["field"])], foreground=[("readonly", th["fg"])],
               selectbackground=[("readonly", th["field"])], selectforeground=[("readonly", th["fg"])])
        st.configure("Vertical.TScrollbar", background=th["panel"], troughcolor=th["bg"], bordercolor=th["bg"],
                     arrowcolor=th["fg"])
        root.option_add("*TCombobox*Listbox.background", th["field"])
        root.option_add("*TCombobox*Listbox.foreground", th["fg"])
        root.option_add("*TCombobox*Listbox.selectBackground", acc)
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        for hook in theme_hooks:
            hook()

    def show_page_ui(i):
        ui["show_page"](i)

    # ---------- 画面の作成 (言語を変えたときは作り直す) ----------
    def build_ui():
        for d in list(ui.get("dlg", {}).values()):
            try:
                d.destroy()
            except Exception:
                pass
        ui["dlg"] = {}
        theme_hooks.clear()
        # ページ切り替え: NC Beats (プレイヤー) / Skin Hitsound / Setting。右上の「•••」から選ぶ
        pager = ttk.Frame(root)
        pager.pack(fill="both", expand=True)
        ui["pager"] = pager
        page_nc = ttk.Frame(pager)
        page_hit = ttk.Frame(pager)
        page_set = ttk.Frame(pager)
        page_skin = ttk.Frame(pager)
        pages = [page_nc, page_hit, page_set, page_skin]
        tab_nc = page_nc

        pv_ref = {}                                  # show_page から、後で作る画面を参照するため

        def show_page(i):
            if i in (1, 3):                            # スキンのページは無い (メトロノーム専用)
                i = 0
            for pg in pages:
                pg.pack_forget()
            pages[i].pack(fill="both", expand=True)
            ui["page"] = i
            for v_ in (pv_ref.get("pv"), pv_ref.get("sv")):
                if v_ is not None and v_.ov:
                    v_.ov_close()
            if i == 2 and pv_ref.get("sv") is not None:
                pv_ref["sv"].schedule()


        def head(parent, text):
            ttk.Label(parent, text=text, style="Head.TLabel").pack(anchor="w", pady=(10, 2))
            ttk.Separator(parent).pack(fill="x", pady=(0, 6))

        def info_row(parent, label, var):
            r = ttk.Frame(parent); r.pack(fill="x", pady=1)
            ttk.Label(r, text=label, style="Muted.TLabel", width=13).pack(side="left")
            ttk.Label(r, textvariable=var, wraplength=330).pack(side="left")

        def slider(parent, text, lo, hi, init, cb, fmt):
            r = ttk.Frame(parent); r.pack(fill="x", pady=4)
            ttk.Label(r, text=text, width=13).pack(side="left")
            lab = ttk.Label(r, width=8, anchor="e"); lab.pack(side="right")
            def f(x):
                x = float(x); lab.config(text=fmt(x)); cb(x)
            s = ttk.Scale(r, from_=lo, to=hi, command=f); s.set(init); s.pack(side="left", fill="x", expand=True, padx=6)
            f(init)

        # =====================================================
        # タブ1: NC Beats (音楽プレイヤー風。背景のグラデーションと光は、自前で描画)
        # =====================================================
        scales = [0.5, 1.0, 2.0]
        v_conn, v_map, v_state, v_snd, v_bg = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        ui["cover_id"] = -1
        font_cache = {}

        def th_now():
            return THEMES.get(cfg["theme"], THEMES["dark"])

        def fam_now():
            return tkfont.nametofont("TkDefaultFont").actual("family")

        def measure(text, size, bold):
            key = (fam_now(), size, bold)
            if key not in font_cache:
                font_cache[key] = tkfont.Font(family=key[0], size=size, weight="bold" if bold else "normal")
            return font_cache[key].measure(text)

        def photo(im):
            from PIL import ImageTk
            return ImageTk.PhotoImage(im)

        def new_dialog(key, title):
            d = ui["dlg"].get(key)
            if d is not None:
                try:
                    if d.winfo_exists():
                        d.lift(); return None
                except Exception:
                    pass
            top = tk.Toplevel(root)
            top.title(title); top.configure(bg=th_now()["bg"]); top.transient(root); top.resizable(False, False)
            ui["dlg"][key] = top
            return top

        def style_dialogs():
            for d in ui["dlg"].values():
                try:
                    d.configure(bg=th_now()["bg"])
                except Exception:
                    pass
        theme_hooks.append(style_dialogs)

        def open_conn():
            top = new_dialog("conn", tr("dlg.conn"))
            if top is None: return
            f = ttk.Frame(top, padding=16); f.pack(fill="both", expand=True)
            info_row(f, tr("lbl.tosu"), v_conn)
            info_row(f, tr("lbl.state"), v_state)
            info_row(f, tr("lbl.map"), v_map)
            info_row(f, tr("lbl.loaded"), v_snd)
            info_row(f, tr("lbl.bg"), v_bg)

        # ---- 半透明の丸いパネル (拍子 / 鳴らす音 / 接続状態 / メニュー)。下に伸びるように開く ----
        meter_names = {m: (tr("meter.auto") if m == "auto" else f"{m}/4") for m in METERS}
        def open_meter(view, anchor):
            rows = [{"label": meter_names[m], "key": m, "check": str(cfg["meter_mode"]) == m} for m in METERS]
            def pick(row):
                cfg["meter_mode"] = row["key"]; engine.rebuild(); upd()
            view.ov_open("menu", None, rows, anchor, pick, width=200)

        def open_sounds(view, anchor):
            names = ("kick", "clap", "hat", "finish") + (("count",) if any(f"beat{i}" in bank.sounds for i in range(1, 9)) else ())
            rows = [{"label": tr("snd.count") if n == "count" else n, "key": n, "check": cfg["enabled_sounds"].get(n, True), "keep": True,
                     "preview": (lambda n=n: bank.play("beat1" if n == "count" else n))} for n in names]
            rows.append({"sep": True})
            rows.append({"label": tr("chk.select"), "key": "sel", "check": "selectplay" in cfg["play_states"], "keep": True})
            rows.append({"label": tr("btn.reload_sounds"), "key": "reload"})
            def pick(row):
                k = row.get("key")
                if k in names:
                    cfg["enabled_sounds"][k] = not cfg["enabled_sounds"].get(k, True); row["check"] = cfg["enabled_sounds"][k]
                    if k == "count":
                        engine.rebuild()
                    return "keep"
                if k == "sel":
                    st = [x for x in cfg["play_states"] if x != "selectplay"]
                    if not row["check"]: st.append("selectplay")
                    cfg["play_states"] = st; row["check"] = not row["check"]
                    return "keep"
                if k == "reload":
                    v_snd.set(", ".join(bank.reload()) or tr("snd.none"))
                    engine.rebuild()
            view.ov_open("menu", tr("dlg.sounds"), rows, anchor, pick, width=290)

        def open_conn_panel(view):
            rows = [{"label": tr("lbl.tosu"), "value": v_conn.get()}, {"label": tr("lbl.state"), "value": v_state.get()},
                    {"label": tr("lbl.map"), "value": v_map.get()}, {"label": tr("lbl.loaded"), "value": v_snd.get()},
                    {"label": tr("lbl.bg"), "value": v_bg.get()}]
            view.ov_open("info", tr("dlg.conn"), rows, ("center",), None, width=340)

        # 右上「•••」のメニュー: ページ移動 / 接続状態 / フルスクリーン
        var_full = tk.BooleanVar(value=bool(ui.get("full", False)))   # 画面を作り直しても、枠なしの状態を保つ
        saved = ui.setdefault("saved_geo", {})
        def set_borderless(on):
            """フルスクリーン = ウィンドウの大きさのまま、タイトルバー (アプリ名・最小化など) だけを隠す"""
            root.update_idletasks()
            if on and not var_full.get():
                saved["geo"] = (root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height())
                cx, cy, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
                root.overrideredirect(True)
                root.geometry(f"{w}x{h}+{cx}+{cy}")          # 中身が動かないように、同じ位置・同じ大きさ
                var_full.set(True)
                try:
                    root.attributes("-transparentcolor", TRANSPARENT_KEY)   # Windows: 角を丸く
                    rad = 22
                except Exception:
                    rad = 0
                ui["full"], ui["rad"] = True, rad
                for v_ in (pv, sv):
                    v_.radius = rad; v_.bg_key = None
                pv.redraw()
                root.focus_force()
            elif not on and var_full.get():
                ui["full"], ui["rad"] = False, 0
                for v_ in (pv, sv):
                    v_.radius = 0; v_.bg_key = None
                pv.redraw()
                try:
                    root.attributes("-transparentcolor", "")
                except Exception:
                    pass
                root.overrideredirect(False)
                x, y, w, h = saved.get("geo", (100, 100, 420, 748))
                root.geometry(f"{w}x{h}+{x}+{y}")
                var_full.set(False)
        ui["borderless"] = set_borderless
        def toggle_full(_e=None):
            set_borderless(not var_full.get())
        def exit_full(_e=None):
            set_borderless(False)
        def quit_app():
            close()
        # 枠なしのときは、画面の端をつまんで大きさを変えられる (四辺と四隅)。何もない所をつかむと、ウィンドウを動かせる
        drag = {}
        EDGE, CORNER, MINW, MINH = 8, 20, 340, 560
        def edge_at(xr, yr):
            x, y = xr - root.winfo_rootx(), yr - root.winfo_rooty()
            w, h = root.winfo_width(), root.winfo_height()
            ed = ("n" if y < EDGE else "s" if y >= h - EDGE else "") + ("w" if x < EDGE else "e" if x >= w - EDGE else "")
            if len(ed) < 2:                       # 四隅は、角の丸みで見えない部分が多いので、広めにつかめるようにする (斜めの変更)
                vy = "n" if y < CORNER else "s" if y >= h - CORNER else ""
                hx = "w" if x < CORNER else "e" if x >= w - CORNER else ""
                if vy and hx:
                    ed = vy + hx
            return ed
        EDGE_CURSOR = {"n": "sb_v_double_arrow", "s": "sb_v_double_arrow", "e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
                       "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw"}
        def on_motion(e):
            if not var_full.get() or drag.get("rs"):
                return
            try:
                root.configure(cursor=EDGE_CURSOR.get(edge_at(e.x_root, e.y_root), ""))
            except Exception:
                pass
        root.bind_all("<Motion>", on_motion)
        def bg_down(e):
            drag["p"] = (e.x_root - root.winfo_x(), e.y_root - root.winfo_y())
            drag["rs"] = None
            if var_full.get():
                ed = edge_at(e.x_root, e.y_root)
                if ed:
                    drag["rs"] = (ed, e.x_root, e.y_root, root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height())
        def bg_move(e):
            if not var_full.get():
                return
            rs = drag.get("rs")
            if rs:
                ed, x0, y0, X, Y, Wd, Hd = rs
                dx, dy = e.x_root - x0, e.y_root - y0
                nx, ny, nw, nh = X, Y, Wd, Hd
                if "e" in ed: nw = Wd + dx
                if "w" in ed: nw, nx = Wd - dx, X + dx
                if "s" in ed: nh = Hd + dy
                if "n" in ed: nh, ny = Hd - dy, Y + dy
                if nw < MINW:
                    if "w" in ed: nx -= MINW - nw
                    nw = MINW
                if nh < MINH:
                    if "n" in ed: ny -= MINH - nh
                    nh = MINH
                root.geometry(f"{nw}x{nh}+{nx}+{ny}")
            elif "p" in drag:
                root.geometry(f"+{e.x_root - drag['p'][0]}+{e.y_root - drag['p'][1]}")
        def open_main(view, anchor):               # プレイヤー / Skin Hitsound 画面の「•••」
            cur = ui.get("page", 0)
            rows = [{"label": tr("tab.nc"), "key": "p0", "check": cur == 0}] + [
                    {"label": tr("tab.set"), "key": "p2", "check": cur == 2}, {"sep": True},
                    {"label": tr("dlg.conn"), "key": "conn"}, {"label": tr("menu.full"), "key": "full", "check": var_full.get()},
                    {"label": tr("menu.quit"), "key": "quit"}]
            def pick(row):
                k = row["key"]
                if k in ("p0", "p1", "p2", "p3"):
                    view.ov_close(); show_page(int(k[1])); return "keep"
                if k == "conn": open_conn_panel(view)
                elif k == "full": toggle_full()
                elif k == "quit": quit_app()
            view.ov_open("menu", None, rows, anchor, pick, width=230)

        def on_escape(_e=None):
            for v_ in (pv, sv):
                if v_.ov:
                    v_.ov_close(); return
            exit_full()
        root.bind("<F11>", toggle_full)
        root.bind("<Escape>", on_escape)

        def step_scale(step):
            cur = min(range(3), key=lambda i: abs(scales[i] - float(cfg["bpm_scale"])))
            cfg["bpm_scale"] = scales[max(0, min(2, cur + step))]
            engine.rebuild(); upd()
        def toggle_on():
            cfg["beats_on"] = not cfg.get("beats_on", True); pv.redraw()
        def set_vol(v):
            cfg["volume"] = v; bank.set_volume(v)
        def set_tim(v):
            cfg["offset_ms"] = int(v)

        canvas_nc = AACanvas(tab_nc, highlightthickness=0, bd=0)
        canvas_nc.pack(fill="both", expand=True)
        pv = PlayerView(canvas_nc, cfg, th_now, fam_now, measure, photo,
                        {"toggle_on": toggle_on, "step_scale": step_scale, "meter": open_meter, "sounds": open_sounds,
                         "bg_down": bg_down, "bg_move": bg_move, "menu": open_main, "full": toggle_full, "set_vol": set_vol, "set_tim": set_tim}, tr)
        theme_hooks.append(pv.redraw)
        ui["pv"] = pv
        pv_ref["pv"] = pv

        def state_text(st):
            key = "state." + st
            return tr(key) if key in STR_EN else st

        def upd():
            """画面の表示を、エンジンの状態に合わせて更新する"""
            st = engine.status
            conn_key = st["conn"]
            v_conn.set(tr(conn_key))
            v_state.set(st["state"])
            title = st.get("map") or ""
            ok = st.get("map_ok")
            v_map.set((title or "-") + (f" ({st['events']})" if ok else (" - " + tr("map.fail") if ok is False else "")))
            color = {"conn.ok": "#2fbf85", "conn.connecting": "#ffb020"}.get(conn_key, "#ff5a5f")
            cur = min(range(3), key=lambda i: abs(scales[i] - float(cfg["bpm_scale"])))
            mc = str(cfg["meter_mode"])
            if engine.bg_id != ui["cover_id"]:
                ui["cover_id"] = engine.bg_id
                raw = engine.bg_bytes
                pv.cover_src = load_cover_src(raw) if raw else None
                pv.cover_id = engine.bg_id
                try:
                    import PIL  # noqa
                    pil_ok = True
                except Exception:
                    pil_ok = False
                if pv.cover_src is not None:
                    v_bg.set(tr("bg.ok", *pv.cover_src.size))
                elif raw and not pil_ok:
                    v_bg.set(tr("bg.nopil"))
                elif raw:
                    v_bg.set(tr("bg.bad"))
                elif engine.status.get("map"):
                    v_bg.set(tr("bg.nofile", getattr(engine, "bg_note", "")))
                else:
                    v_bg.set(tr("bg.wait"))
                pv.bg_key = None
                pv.redraw()
                for v_ in (sv,):                          # Setting 画面の背景も、同じ色に
                    v_.cover_src, v_.cover_id, v_.bg_key = pv.cover_src, pv.cover_id, None
                if ui.get("page") == 2:
                    sv.redraw()
            pv.set_state(head=state_text(st["state"]), title=title or tr("player.notitle"),
                         sub=" · ".join(x for x in (st.get("artist"), f"[{st['version']}]" if st.get("version") else "") if x),
                         dot=color, scale=scales[cur], meter="AUTO" if mc == "auto" or mc not in METERS else f"{mc}/4")
        ui["upd"] = upd
        ui["show_page"] = show_page
        show_page(0)
        v_snd.set(", ".join(bank.loaded()) or tr("snd.none"))


        # =====================================================
        # タブ3: Setting (言語 / デザイン / 動作)  ※同じデザインの、半透明パネルの画面
        # =====================================================
        canvas_set = AACanvas(page_set, highlightthickness=0, bd=0)
        canvas_set.pack(fill="both", expand=True)
        lang_codes = ["auto"] + sorted(LANGS)
        lang_names = {c: (tr("lang.auto") if c == "auto" else LANG_NAMES.get(c, c)) for c in lang_codes}

        def rebuild_all():
            idx = ui.get("page", 0)
            ui["pager"].destroy()
            build_ui()
            show_page_ui(idx)
            tick_once()

        def pick_language(view, anchor):
            rows = [{"label": lang_names[c], "key": c, "check": cfg["language"] == c} for c in lang_codes]
            def pick(row):
                cfg["language"] = row["key"]
                set_language(cfg["language"])
                root.after(40, rebuild_all)            # 画面を作り直す (この操作が終わってから)
            view.ov_open("menu", tr("lbl.language"), rows, anchor, pick, width=250)

        def pick_theme(view, anchor):
            rows = [{"label": tr("theme.dark"), "key": "dark", "check": cfg["theme"] == "dark"},
                    {"label": tr("theme.light"), "key": "light", "check": cfg["theme"] == "light"}]
            def pick(row):
                cfg["theme"] = row["key"]; apply_theme(); refresh_settings()
            view.ov_open("menu", tr("lbl.theme"), rows, anchor, pick, width=220)

        def pick_accent(view, anchor):
            def pick(res):
                if res.get("final") and res.get("color"):
                    cfg["accent"] = res["color"]; apply_theme(); refresh_settings()
            view.ov_open("color", tr("lbl.accent"), None, ("center",), pick, width=330, color=cfg["accent"],
                         presets=list(ACCENTS.values()), hint=tr("hint.palette"))

        fam_list = sorted({f for f in tkfont.families(root) if not f.startswith("@")})
        def pick_font(view, anchor):
            rows = [{"label": f, "key": f, "check": cfg["font_family"] == f} for f in fam_list]
            def pick(row):
                cfg["font_family"] = row["key"]; apply_fonts(); apply_theme(); refresh_settings()
            view.ov_open("menu", tr("lbl.font"), rows, anchor, pick, width=290)

        def set_fsize(v):
            cfg["font_size"] = int(v)
        def done_fsize():
            apply_fonts(); apply_theme(); refresh_settings()

        def set_smooth(v):
            cfg["smooth"] = v
            for fn_ in list(theme_hooks):
                try: fn_()
                except Exception: pass
        def set_top(v):
            cfg["always_on_top"] = v
            root.attributes("-topmost", bool(v or cfg.get("hide_in_play")) and not ui.get("hidden"))
        def set_hideplay(v):
            cfg["hide_in_play"] = v
            vis_tick(force=True)
        SIZES = [("S", 360, 640), ("M", 420, 748), ("L", 480, 854), ("XL", 600, 1067)]
        def pick_winsize(view, anchor):
            rows = [{"label": f"{n}   {w} × {h}", "key": (w, h), "check": (root.winfo_width(), root.winfo_height()) == (w, h)} for n, w, h in SIZES]
            rows.append({"label": tr("size.fit"), "key": "fit"})
            def pick(row):
                k = row["key"]
                if k == "fit":                                   # 画面の高さの9割 (縦横比は、スマホと同じ)
                    h = max(560, int(root.winfo_screenheight() * 0.9)); w = max(340, int(h * 0.56))
                else:
                    w, h = k
                pos = f"+{root.winfo_x()}+{root.winfo_y()}" if var_full.get() else ""
                root.geometry(f"{w}x{h}{pos}")
                root.after(120, refresh_settings)
            view.ov_open("menu", tr("lbl.winsize"), rows, anchor, pick, width=260)

        def set_mincon(v):
            cfg["minimize_console"] = v
        def set_startfull(v):
            cfg["start_fullscreen"] = v
        def set_auto(v):
            try:
                set_autostart(v)
            except Exception as e:
                messagebox.showerror(tr("err.title"), tr("err.autostart", e))
                refresh_settings()
        def set_tosu_auto(v):
            cfg["auto_launch_tosu"] = v

        def run_pick_tosu(view=None, anchor=None):
            p = filedialog.askopenfilename(title=tr("dlg.picktosu"), filetypes=[("tosu", "tosu.exe"), ("exe", "*.exe")])
            if p:
                cfg["tosu_path"] = p
                if cfg["auto_launch_tosu"]: launch_tosu(cfg)
        def run_shortcut(view=None, anchor=None):
            try:
                path = create_desktop_shortcut()
                messagebox.showinfo(APP_TITLE, tr("msg.shortcut", path))
            except Exception as e:
                messagebox.showerror(APP_TITLE, tr("err.shortcut", e))

        def settings_rows():
            sd = cfg.get("skin_dir", "")
            rows = [
                {"kind": "head", "label": tr("lbl.language")},
                {"kind": "menu", "label": tr("lbl.language"), "value": lang_names.get(cfg["language"], lang_names["auto"]), "run": pick_language},
                {"kind": "head", "label": tr("head.design")},
                {"kind": "menu", "label": tr("lbl.theme"), "value": tr("theme." + cfg["theme"]), "run": pick_theme},
                {"kind": "color", "label": tr("lbl.accent"), "value": cfg["accent"], "run": pick_accent},
                {"kind": "menu", "label": tr("lbl.font"), "value": cfg["font_family"], "run": pick_font},
                {"kind": "slider", "label": tr("lbl.fontsize"), "lo": 8, "hi": 16, "val": int(cfg["font_size"]), "int": True,
                 "fmt": (lambda v: f"{int(round(v))} {tr('snd.pt')}"), "on": set_fsize, "done": done_fsize},
                {"kind": "toggle", "label": tr("chk.smooth"), "on": bool(cfg.get("smooth", True)), "set": set_smooth},
                {"kind": "head", "label": tr("head.behavior")},
                {"kind": "toggle", "label": tr("chk.top"), "on": bool(cfg["always_on_top"]), "set": set_top},
                {"kind": "toggle", "label": tr("chk.hideplay"), "on": bool(cfg.get("hide_in_play")), "set": set_hideplay},
                *([{"kind": "toggle", "label": tr("chk.mincon"), "on": bool(cfg.get("minimize_console", True)), "set": set_mincon}] if sys.platform == "win32" else []),
                {"kind": "toggle", "label": tr("chk.startfull"), "on": bool(cfg.get("start_fullscreen")), "set": set_startfull},
                {"kind": "menu", "label": tr("lbl.winsize"), "value": f"{root.winfo_width()}×{root.winfo_height()}", "run": pick_winsize},
            ]
            if sys.platform == "win32":
                rows.append({"kind": "toggle", "label": tr("chk.autostart"), "on": autostart_enabled(), "set": set_auto})
                rows.append({"kind": "button", "label": tr("btn.shortcut"), "run": run_shortcut})
            rows += [
                {"kind": "head", "label": "tosu"},
                {"kind": "toggle", "label": tr("chk.tosu"), "on": bool(cfg["auto_launch_tosu"]), "set": set_tosu_auto},
                {"kind": "button", "label": tr("btn.pick_tosu"), "run": run_pick_tosu},
                {"kind": "button", "label": tr("btn.dl_tosu"), "run": (lambda v=None, a=None: webbrowser.open(TOSU_URL))},
            ]
            return rows

        def refresh_settings():
            sv.set_rows(settings_rows())
        ui["refresh_settings"] = refresh_settings

        sv = SettingView(canvas_set, cfg, th_now, fam_now, measure, photo,
                         {"back": lambda: show_page(0), "menu": open_main, "bg_down": bg_down, "bg_move": bg_move}, tr)
        theme_hooks.append(sv.redraw)
        pv_ref["sv"] = sv
        refresh_settings()
        if var_full.get():                         # 画面を作り直したあとも、枠なしの角の丸みを保つ
            for v_ in (pv, sv):
                v_.radius = ui.get("rad", 0)


    # ---------- 定期更新 ----------
    last = {"skin": None}

    def tick_once():
        ui["upd"]()

    def tick():
        tick_once()
        root.after(500, tick)

    apply_fonts()
    apply_theme()
    build_ui()
    tick()
    if cfg.get("start_fullscreen"):
        root.after(500, lambda: ui["borderless"](True))

    # プレイ中は、ゲームの後ろへ回り、一時停止やリザルトで、また手前に出る (モニター1枚でも、ゲームを隠さない)
    vst = {"last_play": 0.0}
    def vis_tick(force=False):
        try:
            playing = engine.status.get("state") == "play" and not engine.paused
            pin = bool(cfg.get("always_on_top") or cfg.get("hide_in_play"))     # 手前に固定するか
            now = time.time()
            if playing:
                vst["last_play"] = now
            # 少し間をおいて戻す (一瞬の止まりで、ちらつかないように)
            want = bool(cfg.get("hide_in_play")) and (playing or (ui.get("hidden") and now - vst["last_play"] < 0.35))
            if want and not ui.get("hidden"):
                ui["hidden"] = True
                root.attributes("-topmost", False)
                root.lower()
            elif (not want) and (ui.get("hidden") or force):
                ui["hidden"] = False
                root.lift()
                root.attributes("-topmost", True)
                if not pin:
                    root.after(150, lambda: root.attributes("-topmost", False))
            elif (not want) and pin and not playing and not bool(root.attributes("-topmost")):
                root.attributes("-topmost", True)        # 「プレイ中は後ろへ」がONなら、プレイ中以外は、いつも手前に固定する (曲選択で、アプリを触ったあとも)
        except Exception:
            pass
        if not force:
            root.after(250, vis_tick)
    root.after(1000, vis_tick)

    def close():
        try:
            cfg["window_size"] = f"{root.winfo_width()}x{root.winfo_height()}"
        except Exception:
            pass
        engine.stop_flag = True; save_config(cfg); root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


def minimize_console():
    """起動時に出る黒いログ画面を最小化する (Windowsのみ。失敗しても無視)"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nogui", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    if cfg.get("minimize_console", True):
        minimize_console()
    folder = cfg["sounds_dir"] if os.path.isabs(cfg["sounds_dir"]) else os.path.join(HERE, cfg["sounds_dir"])
    bank = SoundBank(folder)
    bank.set_volume(cfg["volume"])
    if cfg["auto_launch_tosu"]:
        if launch_tosu(cfg):
            print("[tosu] 起動済み、または起動しました")
        else:
            print("[tosu] tosu.exe が見つかりません。手動で起動するか、画面から場所を指定してください")
    engine = Engine(cfg, bank)
    engine.start()
    if not args.nogui:
        try:
            import tkinter  # noqa
        except ImportError:
            print("tkinterが無いためGUIなしで起動します")
            args.nogui = True
    if args.nogui:
        print("読込済みの音:", sorted(bank.sounds) or "なし")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        run_gui(engine, bank, cfg)


if __name__ == "__main__":
    main()
