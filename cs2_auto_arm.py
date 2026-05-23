#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CS2 GSI -> 固件 自动装弹桥接（Python 版）

依赖：
    pip install hidapi

运行：
    python gsi_armed_bridge.py
"""

import hid
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# ====== 与固件 / CS2CFG 工具一致的常量 ======
# VENDOR_ID        = 0xCAFE
# PRODUCT_ID       = 0xBAF2
USAGE_PAGE       = 0xFF00
USAGE            = 0x0020
REPORT_ID_CONFIG = 100
CONFIG_SIZE      = 32
CONFIG_VERSION   = 18
SET_AUTO_ARMED   = 26          # 新加的命令号，需与固件一致

# ====== 本地 ======
HTTP_PORT  = 3000
AUTH_TOKEN = 'CS2CFG'   # 必须与 cfg 里 token 一致

# ====== CS2 weapon name → 固件 gun index（0..15）======
# 枪 index 顺序固定 ：
#   0 AK47   1 AUG   2 FAMAS  3 GALIL  4 M4A1-S 5 M4A4   6 SG553
#   7 BIZON  8 MAC10 9 MP5SD  10 MP7   11 MP9   12 P90   13 UMP45
#  14 CZ75   15 M249
WEAPON_MAP = {
    'weapon_ak47':          0,
    'weapon_aug':           1,
    'weapon_famas':         2,
    'weapon_galilar':       3,
    'weapon_m4a1_silencer': 4,    # M4A1-S
    'weapon_m4a1':          5,    # ★ M4A4 的真实 classname 就是 weapon_m4a1（历史遗留坑）
    'weapon_m4a4':          5,    # 保险：万一某些版本 GSI 用这个别名
    'weapon_sg556':         6,    # SG553 的真实 classname 是 sg556
    'weapon_sg553':         6,    # 保险别名
    'weapon_bizon':         7,
    'weapon_mac10':         8,
    'weapon_mp5sd':         9,
    'weapon_mp7':          10,
    'weapon_mp9':          11,
    'weapon_p90':          12,
    'weapon_ump45':        13,
    'weapon_cz75a':        14,
    'weapon_m249':         15,
}

# ====== CRC32（与固件 / 工具相同算法）======
def _make_crc_table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
        t.append(c & 0xFFFFFFFF)
    return t

CRC_TABLE = _make_crc_table()

def crc32(buf, length):
    c = 0xFFFFFFFF
    for i in range(length):
        c = (c >> 8) ^ CRC_TABLE[(c ^ buf[i]) & 0xFF]
    return (c ^ 0xFFFFFFFF) & 0xFFFFFFFF


def build_packet(value):
    """构造 32 字节 SET_AUTO_ARMED 包"""
    buf = bytearray(CONFIG_SIZE)
    buf[0] = CONFIG_VERSION
    buf[1] = SET_AUTO_ARMED
    # i32 little-endian, offset 2
    v = value & 0xFFFFFFFF
    buf[2] = v & 0xFF
    buf[3] = (v >> 8) & 0xFF
    buf[4] = (v >> 16) & 0xFF
    buf[5] = (v >> 24) & 0xFF
    # CRC32 in last 4 bytes
    c = crc32(buf, CONFIG_SIZE - 4)
    buf[CONFIG_SIZE - 4] = c & 0xFF
    buf[CONFIG_SIZE - 3] = (c >> 8) & 0xFF
    buf[CONFIG_SIZE - 2] = (c >> 16) & 0xFF
    buf[CONFIG_SIZE - 1] = (c >> 24) & 0xFF
    return bytes(buf)


# ====== HID ======
_device_lock = threading.Lock()
_device = None

# ====== HID ======
# VID/PID 已经随机化，不再用它们作为筛选条件
# 只靠 usage_page / usage 来认设备（0xFF00 / 0x0020 是自定义 collection，足够独特）

# 可选：如果你固件里设了产品字符串，可以再加一层名字匹配，避免误开其他设备
PRODUCT_NAME_HINT = None   # 例如 "CS2 Macro" / "Pico Macro"，没设就保持 None

def open_device():
    """尝试打开固件，成功返回 True"""
    global _device
    if _device is not None:
        try:
            _device.close()
        except Exception:
            pass
        _device = None

    # 枚举系统里所有 HID 设备
    candidates = list(hid.enumerate())

    # 第一步：按 usage_page + usage 过滤
    targets = [d for d in candidates
               if d.get('usage_page') == USAGE_PAGE and d.get('usage') == USAGE]

    # 第二步（可选）：按产品名再过滤一次，更稳
    if PRODUCT_NAME_HINT:
        named = [d for d in targets
                 if PRODUCT_NAME_HINT.lower() in (d.get('product_string') or '').lower()]
        if named:
            targets = named

    if not targets:
        return False

    # 如果有多个候选，挨个尝试开（有的 collection 在 Windows 上路径不可写）
    for cand in targets:
        try:
            dev = hid.device()
            dev.open_path(cand['path'])
            _device = dev
            try:
                path_str = cand['path'].decode('utf-8', errors='replace')
            except Exception:
                path_str = str(cand['path'])
            vid = cand.get('vendor_id', 0)
            pid = cand.get('product_id', 0)
            name = cand.get('product_string') or ''
            print(f"[hid] opened {name!r} VID={vid:#06x} PID={pid:#06x} path={path_str}")
            return True
        except Exception as e:
            print(f"[hid] open failed on one candidate: {e}")
            continue

    _device = None
    return False

def send_armed(value):
    """发送一个 SET_AUTO_ARMED 包，成功返回 True"""
    global _device
    with _device_lock:
        if _device is None and not open_device():
            return False
        packet = build_packet(value)
        # send_feature_report 第一字节是 report id
        report = [REPORT_ID_CONFIG] + list(packet)
        try:
            _device.send_feature_report(report)
            return True
        except Exception as e:
            print(f"[hid] write failed, will reopen: {e}")
            try:
                _device.close()
            except Exception:
                pass
            _device = None
            return False


# ====== 去重发送 ======
_last_armed = -2  # -2 = 未初始化
_last_lock = threading.Lock()

def set_armed(value):
    global _last_armed
    with _last_lock:
        if value == _last_armed:
            return
        if not send_armed(value):
            return
        _last_armed = value
        label = 'DISARM' if value == -1 else f'gun {value}'
        print(f"[arm] {label}")


# ====== HTTP（GSI 推送）======
class GsiHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length > 0 else b''
        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        # 验 token
        if AUTH_TOKEN:
            token = (data.get('auth') or {}).get('token')
            if token != AUTH_TOKEN:
                self.send_response(401)
                self.end_headers()
                return

        provider = data.get('provider') or {}
        player   = data.get('player')   or {}

        is_me = bool(provider.get('steamid')) and \
                provider.get('steamid') == player.get('steamid')
        playing = player.get('activity') == 'playing'
        state   = player.get('state') or {}
        health  = state.get('health')
        alive   = (health is None) or \
                  (isinstance(health, (int, float)) and health > 0)

        if not (is_me and playing and alive):
            set_armed(-1)
            self.send_response(200)
            self.end_headers()
            return

        weapons = player.get('weapons') or {}
        active = None
        for w in weapons.values():
            if isinstance(w, dict) and w.get('state') == 'active':
                active = w.get('name')
                break

        if not active:
            set_armed(-1)
        elif active in WEAPON_MAP:
            set_armed(WEAPON_MAP[active])
        else:
            # 刀 / 手雷 / 其他不在压枪表里的 → 卸弹
            set_armed(-1)

        self.send_response(200)
        self.end_headers()

    # 屏蔽请求日志（GSI 每 100ms 推一次会刷屏）
    def log_message(self, format, *args):
        pass


def reconnect_loop():
    while True:
        with _device_lock:
            need = _device is None
        if need:
            open_device()
        time.sleep(3)


def main():
    open_device()

    t = threading.Thread(target=reconnect_loop, daemon=True)
    t.start()

    server = HTTPServer(('127.0.0.1', HTTP_PORT), GsiHandler)
    print(f"[gsi] listening on http://127.0.0.1:{HTTP_PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bye]")
        server.shutdown()


if __name__ == '__main__':
    main()