#!/usr/bin/env python3
"""
yb/yb.py - 元宝 Bot 守护进程 (OpenAI 兼容接口)

无交互模式，监听 5000 端口提供 OpenAI 格式 API。
将用户请求转发到微信群聊 @元宝 AI，收到回复后返回。

用法:
    python yb/yb.py

配置文件 yb/config.json:
    APP_ID       - 元宝 Bot APP_KEY（用于签票）
    APP_SECRET   - 元宝 Bot APP_SECRET
    GROUP_CODE   - 目标群号
    YUANBAO_USER_ID - 元宝 AI 的用户 ID
    YUANBAO_NICK    - 元宝 AI 的昵称
    PORT         - 监听端口 (默认 35500)
    debug        - true 时打印所有原始 WebSocket 消息和调试信息
"""

import asyncio
import json
import os
import sys
import time
import uuid
import logging
import random
import re
import hmac
import hashlib
import string

# ── 日志配置 ──
logger = logging.getLogger('yb')
logger.setLevel(logging.INFO)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
logger.addHandler(_sh)

# ── 加载配置 ──
_config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(_config_path, 'r', encoding='utf-8') as f:
    _cfg = json.load(f)

APP_ID = _cfg['APP_ID']
APP_SECRET = _cfg['APP_SECRET']
BOT_ID = _cfg.get('BOT_ID', '')
GROUP_CODE = _cfg['GROUP_CODE']
API_DOMAIN = _cfg.get('API_DOMAIN', 'bot.yuanbao.tencent.com')
WS_URL = _cfg.get('WS_URL', 'wss://bot-wss.yuanbao.tencent.com/wss/connection')
YUANBAO_USER_ID = _cfg.get('YUANBAO_USER_ID',
                                  'szUvRH8s4ekettawNjDREmAG4W7h+Lhb8Sy9tq/otZU=')
YUANBAO_NICK = _cfg.get('YUANBAO_NICK', '元宝')
DEBUG_MODE = _cfg.get('debug', False)
SERVER_PORT = int(_cfg.get('PORT', 35500))
API_KEY = _cfg.get('API_KEY', '')
ADMIN_USERNAME = _cfg.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = _cfg.get('ADMIN_PASSWORD', 'admin123')


def _reload_config():
    """从 config.json 重新加载配置，更新模块级变量"""
    global _cfg, APP_ID, APP_SECRET, BOT_ID, GROUP_CODE, API_DOMAIN, WS_URL
    global YUANBAO_USER_ID, YUANBAO_NICK, DEBUG_MODE, SERVER_PORT, API_KEY
    global ADMIN_USERNAME, ADMIN_PASSWORD
    with open(_config_path, 'r', encoding='utf-8') as f:
        _cfg = json.load(f)
    APP_ID = _cfg['APP_ID']
    APP_SECRET = _cfg['APP_SECRET']
    BOT_ID = _cfg.get('BOT_ID', '')
    GROUP_CODE = _cfg['GROUP_CODE']
    API_DOMAIN = _cfg.get('API_DOMAIN', 'bot.yuanbao.tencent.com')
    WS_URL = _cfg.get('WS_URL', 'wss://bot-wss.yuanbao.tencent.com/wss/connection')
    YUANBAO_USER_ID = _cfg.get('YUANBAO_USER_ID', 'szUvRH8s4ekettawNjDREmAG4W7h+Lhb8Sy9tq/otZU=')
    YUANBAO_NICK = _cfg.get('YUANBAO_NICK', '元宝')
    DEBUG_MODE = _cfg.get('debug', False)
    SERVER_PORT = int(_cfg.get('PORT', 35500))
    API_KEY = _cfg.get('API_KEY', '')
    ADMIN_USERNAME = _cfg.get('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = _cfg.get('ADMIN_PASSWORD', 'admin123')
    logger.info("配置已重载")
    return _cfg


def _save_config(updates: dict) -> None:
    """更新并保存 config.json，同时重载模块级变量"""
    global _cfg
    with open(_config_path, 'r', encoding='utf-8') as f:
        _cfg = json.load(f)
    _cfg.update(updates)
    with open(_config_path, 'w', encoding='utf-8') as f:
        json.dump(_cfg, f, ensure_ascii=False, indent=4)
    _reload_config()
    logger.info(f"配置已保存: {list(updates.keys())}")


# ── 协议常量 ──
CMD_TYPE_REQUEST = 0
CMD_TYPE_RESPONSE = 1
CMD_TYPE_PUSH = 2
CMD_TYPE_PUSH_ACK = 3
CMD_AUTH_BIND = "auth-bind"
CMD_PING = "ping"
MODULE_CONN_ACCESS = "conn_access"
BIZ_MODULE = "yuanbao_openclaw_proxy"


class SimpleProtobufCodec:
    """简化的 Protobuf 编解码器（摘自 client.py）"""

    @staticmethod
    def encode_varint(value: int) -> bytes:
        result = []
        while value > 127:
            result.append((value & 0x7f) | 0x80)
            value >>= 7
        result.append(value)
        return bytes(result)

    @staticmethod
    def decode_varint(data: bytes, pos: int = 0) -> tuple:
        result = 0
        shift = 0
        while True:
            b = data[pos]
            pos += 1
            result |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    @staticmethod
    def encode_string(field: int, value: str) -> bytes:
        tag = (field << 3) | 2
        encoded = value.encode('utf-8')
        return bytes([tag]) + SimpleProtobufCodec.encode_varint(len(encoded)) + encoded

    @staticmethod
    def encode_bytes(field: int, value: bytes) -> bytes:
        tag = (field << 3) | 2
        return bytes([tag]) + SimpleProtobufCodec.encode_varint(len(value)) + value

    @staticmethod
    def encode_uint32(field: int, value: int) -> bytes:
        tag = (field << 3) | 0
        return bytes([tag]) + SimpleProtobufCodec.encode_varint(value)

    @staticmethod
    def encode_message_field(field: int, inner: bytes) -> bytes:
        tag = (field << 3) | 2
        return bytes([tag]) + SimpleProtobufCodec.encode_varint(len(inner)) + inner

    @staticmethod
    def encode_head(cmd_type: int, cmd: str, seq_no: int, msg_id: str, module: str) -> bytes:
        data = b''
        data += SimpleProtobufCodec.encode_uint32(1, cmd_type)
        data += SimpleProtobufCodec.encode_string(2, cmd)
        data += SimpleProtobufCodec.encode_uint32(3, seq_no)
        data += SimpleProtobufCodec.encode_string(4, msg_id)
        data += SimpleProtobufCodec.encode_string(5, module)
        return data

    @staticmethod
    def encode_conn_msg(head: bytes, data: bytes = b'') -> bytes:
        result = SimpleProtobufCodec.encode_message_field(1, head)
        if data:
            result += SimpleProtobufCodec.encode_bytes(2, data)
        return result

    @staticmethod
    def encode_auth_bind_req(biz_id: str, uid: str, source: str, token: str) -> bytes:
        data = SimpleProtobufCodec.encode_string(1, biz_id)
        auth_info = b''
        auth_info += SimpleProtobufCodec.encode_string(1, uid)
        auth_info += SimpleProtobufCodec.encode_string(2, source)
        auth_info += SimpleProtobufCodec.encode_string(3, token)
        data += SimpleProtobufCodec.encode_message_field(2, auth_info)
        return data

    @staticmethod
    def encode_at_element(user_id: str, nickname: str = "") -> bytes:
        """构建 @ 提及的 TIMCustomElem"""
        display = nickname or user_id
        at_data = json.dumps({
            "elem_type": 1002,
            "text": f"@{display}",
            "user_id": user_id
        })
        at_content = SimpleProtobufCodec.encode_string(4, at_data)
        elem = b''
        elem += SimpleProtobufCodec.encode_string(1, "TIMCustomElem")
        elem += SimpleProtobufCodec.encode_message_field(2, at_content)
        return elem

    @staticmethod
    def encode_tim_file_elem(url: str, uuid: str = "", file_size: int = 0, file_name: str = "") -> bytes:
        """编码 TIMFileElem 文件消息元素"""
        msg_content = b''
        if uuid:
            msg_content += SimpleProtobufCodec.encode_string(2, uuid)
        msg_content += SimpleProtobufCodec.encode_string(10, url)
        if file_size:
            msg_content += bytes([(11 << 3) | 0]) + SimpleProtobufCodec.encode_varint(file_size)
        if file_name:
            msg_content += SimpleProtobufCodec.encode_string(12, file_name)
        elem = b''
        elem += SimpleProtobufCodec.encode_string(1, "TIMFileElem")
        elem += SimpleProtobufCodec.encode_message_field(2, msg_content)
        return elem

    @staticmethod
    def encode_send_group_msg_req(msg_id: str, group_code: str,
                                   from_account: str, text: str,
                                   at_user_id: str = "", at_nickname: str = "") -> bytes:
        data = b''
        data += SimpleProtobufCodec.encode_string(1, msg_id)
        data += SimpleProtobufCodec.encode_string(2, group_code)
        data += SimpleProtobufCodec.encode_string(3, from_account)
        data += SimpleProtobufCodec.encode_string(5, str(random.randint(1, 999999999)))
        # 如果有 @ 目标，先添加 TIMCustomElem（艾特元素）
        if at_user_id:
            at_elem = SimpleProtobufCodec.encode_at_element(at_user_id, at_nickname)
            data += SimpleProtobufCodec.encode_message_field(6, at_elem)
        # TIMTextElem（消息文本）
        msg_content = SimpleProtobufCodec.encode_string(1, text)
        msg_body_elem = SimpleProtobufCodec.encode_string(1, "TIMTextElem")
        msg_body_elem += SimpleProtobufCodec.encode_message_field(2, msg_content)
        data += SimpleProtobufCodec.encode_message_field(6, msg_body_elem)
        return data

    # ── 解码方法 ──

    @staticmethod
    def decode_conn_msg(data: bytes) -> dict:
        result = {'head': {}, 'data': b''}
        pos = 0
        while pos < len(data):
            tag = data[pos]
            pos += 1
            field = tag >> 3
            wire = tag & 7
            if field == 1 and wire == 2:
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['head'] = SimpleProtobufCodec._decode_head(data[pos:pos+length])
                pos += length
            elif field == 2 and wire == 2:
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['data'] = data[pos:pos+length]
                pos += length
            else:
                if wire == 0:
                    _, pos = SimpleProtobufCodec.decode_varint(data, pos)
                elif wire == 2:
                    length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                    pos += length
                else:
                    break
        return result

    @staticmethod
    def _decode_head(data: bytes) -> dict:
        result = {}
        pos = 0
        while pos < len(data):
            tag = data[pos]
            pos += 1
            field = tag >> 3
            wire = tag & 7
            if field == 1 and wire == 0:
                result['cmdType'], pos = SimpleProtobufCodec.decode_varint(data, pos)
            elif field == 2 and wire == 2:
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['cmd'] = data[pos:pos+length].decode('utf-8')
                pos += length
            elif field == 3 and wire == 0:
                result['seqNo'], pos = SimpleProtobufCodec.decode_varint(data, pos)
            elif field == 4 and wire == 2:
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['msgId'] = data[pos:pos+length].decode('utf-8')
                pos += length
            elif field == 5 and wire == 2:
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['module'] = data[pos:pos+length].decode('utf-8')
                pos += length
            elif field == 10 and wire == 0:
                result['status'], pos = SimpleProtobufCodec.decode_varint(data, pos)
            else:
                if wire == 0:
                    _, pos = SimpleProtobufCodec.decode_varint(data, pos)
                elif wire == 2:
                    length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                    pos += length
                else:
                    break
        return result

    @staticmethod
    def decode_auth_bind_rsp(data: bytes) -> dict:
        result = {}
        pos = 0
        while pos < len(data):
            tag = data[pos]
            pos += 1
            field = tag >> 3
            wire = tag & 7
            if field == 1 and wire == 0:
                result['code'], pos = SimpleProtobufCodec.decode_varint(data, pos)
            elif field == 2 and wire == 2:
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['message'] = data[pos:pos+length].decode('utf-8')
                pos += length
            elif field == 3 and wire == 2:
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['connectId'] = data[pos:pos+length].decode('utf-8')
                pos += length
            else:
                if wire == 0:
                    _, pos = SimpleProtobufCodec.decode_varint(data, pos)
                elif wire == 2:
                    length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                    pos += length
                else:
                    break
        return result

    @staticmethod
    def decode_inbound_push(data: bytes) -> dict:
        """解码 InboundMessagePush JSON 或 protobuf 格式"""
        # 先尝试 JSON
        try:
            raw = json.loads(data.decode('utf-8'))
            # JSON 格式直接使用 snake_case 字段名（服务器推送的 JSON 用 snake_case）
            result = {}
            result['from_account'] = raw.get('from_account', raw.get('fromAccount', ''))
            result['fromAccount'] = result['from_account']
            result['group_code'] = raw.get('group_code', raw.get('groupCode', ''))
            result['groupCode'] = result['group_code']
            result['msg_id'] = raw.get('msg_id', raw.get('msgId', ''))
            result['msgId'] = result['msg_id']
            # 从 msg_body 数组中提取文本和图片 URL
            msg_body = raw.get('msg_body', [])
            text = ''
            image_urls = []
            for elem in msg_body:
                if not isinstance(elem, dict):
                    continue
                msg_type = elem.get('msg_type', '')
                mcontent = elem.get('msg_content', {})
                if not isinstance(mcontent, dict):
                    continue
                if msg_type == 'TIMTextElem':
                    t = mcontent.get('text', '') or ''
                    if t:
                        text = t
                elif msg_type == 'TIMImageElem':
                    img_array = mcontent.get('image_info_array', [])
                    for img_info in img_array:
                        if isinstance(img_info, dict) and img_info.get('url'):
                            image_urls.append(img_info['url'])
            result['text'] = text
            result['image_urls'] = image_urls
            return result
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        # protobuf 格式解码（只提取关键字段）
        result = {}
        pos = 0
        while pos < len(data):
            tag = data[pos]
            pos += 1
            field = tag >> 3
            wire = tag & 7
            if field == 2 and wire == 2:  # fromAccount
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['from_account'] = data[pos:pos+length].decode('utf-8')
                pos += length
            elif field == 5 and wire == 2:  # groupCode
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['group_code'] = data[pos:pos+length].decode('utf-8')
                pos += length
            elif field == 12 and wire == 2:  # msgBody
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                body_data = data[pos:pos+length]
                pos += length
                # 从 msgBody 中提取文本
                text = SimpleProtobufCodec._extract_text(body_data)
                if text:
                    result['text'] = text
            elif field == 11 and wire == 2:  # msgId
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                result['msg_id'] = data[pos:pos+length].decode('utf-8')
                pos += length
            else:
                if wire == 0:
                    _, pos = SimpleProtobufCodec.decode_varint(data, pos)
                elif wire == 2:
                    length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                    pos += length
                else:
                    break
        result.setdefault('text', '')
        result['image_urls'] = []
        return result

    @staticmethod
    def _extract_text(data: bytes) -> str:
        pos = 0
        while pos < len(data):
            tag = data[pos]
            pos += 1
            field = tag >> 3
            wire = tag & 7
            if field == 2 and wire == 2:  # msgContent
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                content = data[pos:pos+length]
                pos += length
                cpos = 0
                while cpos < len(content):
                    ctag = content[cpos]
                    cpos += 1
                    cfield = ctag >> 3
                    cwire = ctag & 7
                    if cfield == 1 and cwire == 2:  # text
                        tlen, cpos = SimpleProtobufCodec.decode_varint(content, cpos)
                        return content[cpos:cpos+tlen].decode('utf-8')
                    elif cwire == 0:
                        _, cpos = SimpleProtobufCodec.decode_varint(content, cpos)
                    elif cwire == 2:
                        tlen, cpos = SimpleProtobufCodec.decode_varint(content, cpos)
                        cpos += tlen
                    else:
                        break
            elif wire == 0:
                _, pos = SimpleProtobufCodec.decode_varint(data, pos)
            elif wire == 2:
                length, pos = SimpleProtobufCodec.decode_varint(data, pos)
                pos += length
            else:
                break
        return ''

    @staticmethod
    def extract_content_text(content) -> str:
        """从 OpenAI 格式的 content 字段中安全提取纯文本。
        支持三种格式:
        - None/缺失 → ''
        - 字符串 → 原样返回
        - 数组 [{"type":"text","text":"..."}, ...] → 拼接所有 text 元素
        """
        if not content:
            return ''
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'text':
                    texts.append(part.get('text', ''))
            return '\n'.join(texts)
        return str(content)


class YuanbaoClient:
    """元宝 Bot WebSocket 客户端（简化版，专为 yb 守护进程）"""

    def __init__(self):
        self.codec = SimpleProtobufCodec
        self.ws = None
        self.connected = False
        self.seq_no = 0
        self.token: str | None = None
        self.bot_id: str | None = None
        self.connect_id: str | None = None
        self.instance_id = str(random.randint(1, 1000))

    def _generate_msg_id(self) -> str:
        return uuid.uuid4().hex

    def _generate_nonce(self) -> str:
        return ''.join(random.choices(string.hexdigits.lower(), k=32))

    def _get_beijing_time(self) -> str:
        from datetime import timezone, timedelta
        utc = time.time()
        beijing_ts = utc + 8 * 3600
        from datetime import datetime
        return datetime.fromtimestamp(beijing_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+08:00"
        )

    def _generate_signature(self, nonce: str, timestamp: str) -> str:
        plain = f"{nonce}{timestamp}{APP_ID}{APP_SECRET}"
        return hmac.new(
            APP_SECRET.encode(), plain.encode(), hashlib.sha256
        ).hexdigest()

    def sign_token(self):
        """签票获取 token"""
        import requests
        url = f"https://{API_DOMAIN}/api/v5/robotLogic/sign-token"
        nonce = self._generate_nonce()
        timestamp = self._get_beijing_time()
        signature = self._generate_signature(nonce, timestamp)

        headers = {
            "Content-Type": "application/json",
            "X-AppVersion": "1.0.11",
            "X-OperationSystem": "linux",
            "X-Instance-Id": self.instance_id,
            "X-Bot-Version": "2026.3.22"
        }
        body = {
            "app_key": APP_ID,
            "nonce": nonce,
            "signature": signature,
            "timestamp": timestamp
        }

        logger.info(f"签票中...")
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        result = resp.json()

        if result.get('code') == 0:
            data = result['data']
            self.token = data['token']
            self.bot_id = data['bot_id']
            logger.info(f"签票成功, bot_id={self.bot_id}")
        else:
            raise Exception(f"签票失败: {result}")

    async def connect(self):
        """连接 WebSocket 并进行鉴权"""
        import websockets

        if not self.token:
            self.sign_token()

        logger.info(f"连接 WebSocket: {WS_URL}")
        self.ws = await websockets.connect(WS_URL)
        logger.info("WebSocket 连接成功")

        # 发送鉴权
        auth_id = self._generate_msg_id()
        auth_data = self.codec.encode_auth_bind_req(
            "ybBot", self.bot_id or "", "web", self.token or ""
        )
        head = self.codec.encode_head(
            CMD_TYPE_REQUEST, CMD_AUTH_BIND,
            self.seq_no, auth_id, MODULE_CONN_ACCESS
        )
        self.seq_no += 1
        msg = self.codec.encode_conn_msg(head, auth_data)
        await self.ws.send(msg)
        logger.info(f"鉴权消息已发送")

        # 等待鉴权响应
        resp = await self.ws.recv()
        conn = self.codec.decode_conn_msg(resp)
        h = conn['head']

        if h.get('cmd') == CMD_AUTH_BIND:
            rsp = self.codec.decode_auth_bind_rsp(conn['data'])
            code = rsp.get('code', 0)
            if code == 0 or code == 41101:
                self.connect_id = rsp.get('connectId')
                self.connected = True
                logger.info(f"鉴权成功! connectId={self.connect_id}")
            else:
                raise Exception(f"鉴权失败: code={code}, msg={rsp.get('message')}")
        else:
            raise Exception(f"意外响应: cmd={h.get('cmd')}")

    async def send_group_message(self, group_code: str, text: str,
                                  at_user_id: str = "", at_nickname: str = "") -> bool:
        if not self.ws or not self.connected:
            logger.warning("发送失败: 未连接")
            return False

        msg_id = self._generate_msg_id()
        biz_data = self.codec.encode_send_group_msg_req(
            msg_id, group_code, self.bot_id or "", text,
            at_user_id=at_user_id, at_nickname=at_nickname
        )
        head = self.codec.encode_head(
            CMD_TYPE_REQUEST, "send_group_message",
            self.seq_no, msg_id, BIZ_MODULE
        )
        self.seq_no += 1
        msg = self.codec.encode_conn_msg(head, biz_data)
        await self.ws.send(msg)
        logger.info(f"群消息已发送: {text[:60]}")
        return True

    # ── 文件发送支持 ──

    def _get_upload_info(self, filename: str, file_id: str) -> dict | None:
        """获取文件上传凭证"""
        import requests
        if not self.bot_id or not self.token:
            logger.warning("获取上传凭证失败: 未获取到 bot_id 或 token")
            return None
        if not file_id:
            file_id = uuid.uuid4().hex
        url = f"https://{API_DOMAIN}/api/resource/genUploadInfo"
        headers = {
            "Content-Type": "application/json",
            "X-ID": self.bot_id,
            "X-Token": self.token,
            "X-Source": "web",
            "X-AppVersion": "2.0.1",
            "X-OperationSystem": "Linux",
            "X-Instance-Id": "99",
        }
        body = {
            "fileName": filename,
            "fileId": file_id,
            "docFrom": "localDoc",
            "docOpenId": ""
        }
        try:
            response = requests.post(url, headers=headers, json=body, timeout=30)
            result = response.json()
            if result.get("code", 0) == 0:
                return result.get("data", result)
            else:
                logger.warning(f"获取上传凭证失败: {result}")
                return None
        except Exception as e:
            logger.warning(f"获取上传凭证错误: {e}")
            return None

    def resolve_image_url(self, resource_url: str) -> str | None:
        """将元宝图片资源保护 URL（/api/resource/download?resourceId=xxx）转换为 COS 预签名直链

        流程：
        1. 从 URL 中提取 resourceId
        2. 调用 /api/resource/v1/download 接口（带上 X-ID + X-Token 鉴权头）
        3. 返回 COS 预签名直链（可直接 curl/wget 下载）
        """
        import requests as _requests

        # 从 URL 中提取 resourceId
        match = re.search(r'resourceId=([^&]+)', resource_url)
        if not match:
            logger.warning(f"无法从 URL 中提取 resourceId: {resource_url}")
            return None

        resource_id = match.group(1)
        url = f"https://{API_DOMAIN}/api/resource/v1/download?resourceId={resource_id}"
        headers = {
            "X-ID": BOT_ID or self.bot_id or "",
            "X-Token": self.token or "",
            "X-Source": "web",
            "X-AppVersion": "1.0.11",
            "X-OperationSystem": "linux",
            "X-Instance-Id": self.instance_id,
            "X-Bot-Version": "2026.3.22",
        }

        try:
            resp = _requests.get(url, headers=headers, timeout=30)
            result = resp.json()
            # API 直接返回 COS 预签名直链在顶层（如 {"realUrl": "https://..."}）
            cos_url = result.get("realUrl") or result.get("url") or ""
            if cos_url and cos_url.startswith("http"):
                logger.info(f"图片直链获取成功: {cos_url[:80]}...")
                return cos_url
            # 也可能包装在 code/data 结构中
            if result.get("code", 0) == 0:
                data = result.get("data", {})
                cos_url = data.get("url") or data.get("realUrl") or ""
                if cos_url:
                    logger.info(f"图片直链获取成功: {cos_url[:80]}...")
                    return cos_url
            logger.warning(f"获取图片直链失败: {result}")
            return None
        except Exception as e:
            logger.warning(f"获取图片直链错误: {e}")
            return None

    def _upload_to_cos(self, config: dict, data: bytes, filename: str) -> str | None:
        """上传文件到腾讯云 COS"""
        try:
            from qcloud_cos import CosConfig, CosS3Client
            cos_config = CosConfig(
                Region=config["region"],
                SecretId=config["encryptTmpSecretId"],
                SecretKey=config["encryptTmpSecretKey"],
                Token=config["encryptToken"],
            )
            client = CosS3Client(cos_config)
            client.put_object(
                Bucket=config["bucketName"],
                Body=data,
                Key=config["location"],
                ContentType="application/octet-stream",
            )
            return config.get("resourceUrl",
                              f"https://{config['bucketName']}.cos.{config['region']}.myqcloud.com{config['location']}")
        except ImportError:
            # 手动签名回退
            secret_id = config.get("encryptTmpSecretId", "")
            secret_key = config.get("encryptTmpSecretKey", "")
            security_token = config.get("encryptToken", "")
            start_time = config.get("startTime", 0)
            expired_time = config.get("expiredTime", 0)
            bucket = config.get("bucketName", "")
            region = config.get("region", "")
            location = config.get("location", "")
            key_time = f"{start_time};{expired_time}"
            sign_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
            http_string = f"put\n{location}\n\nhost={bucket}.cos.{region}.myqcloud.com\n"
            string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
            signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
            authorization = (f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}"
                             f"&q-key-time={key_time}&q-header-list=host&q-url-param-list=&q-signature={signature}")
            if security_token:
                authorization += f"&x-cos-security-token={security_token}"
            upload_url = f"https://{bucket}.cos.{region}.myqcloud.com{location}"
            headers = {
                "Host": f"{bucket}.cos.{region}.myqcloud.com",
                "Authorization": authorization,
                "Content-Type": "application/octet-stream",
            }
            if security_token:
                headers["x-cos-security-token"] = security_token
            try:
                import requests
                response = requests.put(upload_url, headers=headers, data=data, timeout=60)
                if response.status_code == 200:
                    return config.get("resourceUrl", upload_url)
                else:
                    logger.warning(f"上传失败: {response.status_code} {response.text[:200]}")
                    return None
            except Exception as e:
                logger.warning(f"上传错误: {e}")
                return None
        except Exception as e:
            logger.warning(f"上传到 COS 失败: {e}")
            return None

    def _build_file_msg(self, url: str, uuid: str = "", file_size: int = 0, file_name: str = "") -> bytes:
        """构建文件群消息（TIMFileElem）"""
        file_elem = self.codec.encode_tim_file_elem(url, uuid, file_size, file_name)
        data = b''
        data += self.codec.encode_string(1, self._generate_msg_id())           # msg_id
        data += self.codec.encode_string(2, GROUP_CODE)                        # group_code
        data += self.codec.encode_string(3, self.bot_id or "")                 # from_account
        data += self.codec.encode_string(4, "")                                # to_account（空）
        data += self.codec.encode_string(5, str(random.randint(1, 999999999))) # random
        data += self.codec.encode_message_field(6, file_elem)                  # msgBody
        data += self.codec.encode_string(7, "")                                # refMsgId（空）
        # 构建 ConnMsg
        seq_no = self.seq_no
        self.seq_no += 1
        msg_id = self._generate_msg_id()
        head = self.codec.encode_head(
            CMD_TYPE_REQUEST, "send_group_message", seq_no,
            msg_id, BIZ_MODULE
        )
        return self.codec.encode_conn_msg(head, data)

    async def send_file(self, file_path: str) -> bool:
        """发送文件消息（COS 上传 + TIMFileElem）"""
        if not self.connected or not self.ws:
            logger.warning("发送文件失败: 未连接")
            return False

        import os.path
        if not os.path.exists(file_path):
            logger.warning(f"文件不存在: {file_path}")
            return False

        try:
            with open(file_path, 'rb') as f:
                data = f.read()
        except Exception as e:
            logger.warning(f"读取文件失败: {e}")
            return False

        max_bytes = 20 * 1024 * 1024
        if len(data) > max_bytes:
            logger.warning(f"文件过大: {len(data) / 1024 / 1024:.1f} MB > 20 MB")
            return False

        filename = os.path.basename(file_path)
        file_id = uuid.uuid4().hex
        config = self._get_upload_info(filename, file_id)
        if not config:
            return False

        url = self._upload_to_cos(config, data, filename)
        if not url:
            return False

        try:
            msg = self._build_file_msg(url, file_id, file_size=len(data), file_name=filename)
            await self.ws.send(msg)
            logger.info(f"文件已发送: {filename} ({len(data)} bytes)")
            return True
        except Exception as e:
            logger.warning(f"发送文件失败: {e}")
            return False

    async def _send_ping(self):
        ping_id = self._generate_msg_id()
        head = self.codec.encode_head(
            CMD_TYPE_REQUEST, CMD_PING,
            self.seq_no, ping_id, MODULE_CONN_ACCESS
        )
        self.seq_no += 1
        msg = self.codec.encode_conn_msg(head, b'')
        await self.ws.send(msg)

    async def _send_ack(self, head: dict):
        ack_head = self.codec.encode_head(
            CMD_TYPE_PUSH_ACK,
            head.get('cmd', ''),
            self.seq_no,
            head.get('msgId', ''),
            head.get('module', '')
        )
        self.seq_no += 1
        msg = self.codec.encode_conn_msg(ack_head)
        await self.ws.send(msg)

    async def disconnect(self):
        self.connected = False
        if self.ws:
            await self.ws.close()
            self.ws = None
        logger.info("已断开连接")


class YBDaemon:
    """元宝守护进程 - 管理连接、消息转发和 HTTP API"""

    def __init__(self):
        self.client = YuanbaoClient()
        self._pending_future: asyncio.Future | None = None
        self._connected = asyncio.Event()
        self._running = True
        self._http_server = None
        # 用于缓冲元宝的连续推送（文字+图片），使用 debounce 合并后 resolve
        self._reply_buffer: list[dict] = []
        self._reply_timer: asyncio.Task | None = None
        # ── 管理员会话 ──
        self._sessions: dict[str, float] = {}  # token -> created_time

        # 启动时清理旧文件
        base_dir = os.path.dirname(os.path.abspath(__file__))
        for fname in ('历史.txt', '工具.txt'):
            fpath = os.path.join(base_dir, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
                logger.info(f"启动清理: 已删除旧文件 {fname}")

    # ── 管理员会话管理 ──

    def _generate_session(self) -> str:
        token = uuid.uuid4().hex
        self._sessions[token] = time.time()
        return token

    def _verify_session(self, cookies: dict) -> bool:
        token = cookies.get('yb_admin_token', '')
        if token in self._sessions:
            created = self._sessions[token]
            # 会话 24 小时有效
            if time.time() - created < 86400:
                return True
            del self._sessions[token]
        return False

    @staticmethod
    def _parse_cookies(header_str: str) -> dict:
        result = {}
        for part in header_str.split(';'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                result[k.strip()] = v.strip()
        return result

    @staticmethod
    def _html_response(code: int, html: str, extra_headers: str = '') -> bytes:
        body = html.encode('utf-8')
        status = {200: 'OK', 302: 'Found', 401: 'Unauthorized', 404: 'Not Found'}
        resp = (
            f"HTTP/1.1 {code} {status.get(code, 'OK')}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"{extra_headers}"
            f"\r\n"
        ).encode('utf-8') + body
        return resp

    # ── 管理页面 HTML（从文件读取） ──

    @staticmethod
    def _read_template(name: str) -> str:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    @staticmethod
    def _render_admin_page(page_type: str, connected: bool = False, error: str = '') -> str:
        html = YBDaemon._read_template('admin.html')
        if page_type == 'login':
            html = html.replace('{LOGIN_DISPLAY}', 'block')
            html = html.replace('{ADMIN_DISPLAY}', 'none')
            html = html.replace('{TITLE}', '管理员登录 - 元宝 Bot')
            err_block = f'<div class="msg error">{error}</div>' if error else ''
            html = html.replace('{LOGIN_ERROR_BLOCK}', err_block)
            html = html.replace('{STATUS_CLASS}', '')
            html = html.replace('{STATUS_TEXT}', '')
        else:
            html = html.replace('{LOGIN_DISPLAY}', 'none')
            html = html.replace('{ADMIN_DISPLAY}', 'block')
            html = html.replace('{TITLE}', '管理面板 - 元宝 Bot')
            html = html.replace('{LOGIN_ERROR_BLOCK}', '')
            status_class = 'connected' if connected else 'disconnected'
            status_text = '已连接' if connected else '未连接'
            html = html.replace('{STATUS_CLASS}', status_class)
            html = html.replace('{STATUS_TEXT}', status_text)
        return html

    async def _heartbeat_loop(self):
        while self._running and self.client.connected:
            await asyncio.sleep(10)
            try:
                await self.client._send_ping()
                logger.debug("心跳 Ping 已发送")
            except Exception as e:
                logger.error(f"心跳失败: {e}")
                self.client.connected = False
                break

    async def _receive_loop(self):
        """消息接收循环 - 检测元宝的回复"""
        import websockets
        try:
            async for raw in self.client.ws:
                try:
                    if DEBUG_MODE:
                        logger.info(f"🔍 [DEBUG] 原始消息 ({len(raw)} bytes):\n{raw.hex()}")
                    conn = self.client.codec.decode_conn_msg(raw)
                    head = conn['head']
                    if DEBUG_MODE:
                        logger.info(f"🔍 [DEBUG] 消息头: cmdType={head.get('cmdType')} needAck={head.get('needAck')}")
                    cmd_type = head.get('cmdType')

                    # ACK
                    if head.get('needAck'):
                        await self.client._send_ack(head)

                    if cmd_type == CMD_TYPE_RESPONSE:
                        continue  # 忽略响应

                    if cmd_type != CMD_TYPE_PUSH:
                        continue

                    # 解析推送消息
                    data = conn.get('data', b'')
                    if not data:
                        continue

                    inbound = self.client.codec.decode_inbound_push(data)
                    from_account = inbound.get('from_account', '')
                    group_code = inbound.get('group_code', '')
                    text = inbound.get('text', '')

                    if DEBUG_MODE:
                        logger.info(f"🔍 [DEBUG] 解码结果: from={from_account} group={group_code} "
                                    f"text='{text[:80]}' images={len(inbound.get('image_urls', []))} "
                                    f"msgBody_keys={[k for k in inbound.keys() if 'msg' in k.lower() or 'body' in k.lower()]}")
                    logger.debug(f"收到消息 from={from_account} group={group_code} text={text[:40]}")

                    # 关键：检测是否是目标群中元宝的回复
                    if (group_code == GROUP_CODE
                            and from_account == YUANBAO_USER_ID
                            and self._pending_future is not None
                            and not self._pending_future.done()):
                        image_urls = inbound.get('image_urls', [])
                        self._reply_buffer.append({
                            'text': text,
                            'image_urls': image_urls,
                        })
                        log_parts = []
                        if text:
                            log_parts.append(f"text='{text[:60]}'")
                        if image_urls:
                            log_parts.append(f"images={len(image_urls)}张")
                        logger.info(f"收到元宝回复: {', '.join(log_parts)}")

                        # 取消旧 timer，重新设置 debounce
                        if self._reply_timer:
                            self._reply_timer.cancel()
                        self._reply_timer = asyncio.create_task(
                            self._flush_reply_debounce()
                        )

                except Exception as e:
                    logger.error(f"消息处理异常: {e}")

        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket 连接关闭")
        except Exception as e:
            logger.error(f"接收循环异常: {e}")
        finally:
            self.client.connected = False
            self._running = False
            if self._pending_future and not self._pending_future.done():
                self._pending_future.set_exception(Exception("连接断开"))

    async def _flush_reply_debounce(self):
        """等待一小段时间后合并缓冲区的所有回复（文字+图片），然后 resolve future"""
        try:
            await asyncio.sleep(0.3)  # 300ms debounce，等待连续推送
        except asyncio.CancelledError:
            return  # 新的推送来了，取消当前 timer

        # 合并缓冲区
        combined_text = ''
        combined_images = []
        for part in self._reply_buffer:
            if part['text']:
                if combined_text:
                    combined_text += '\n'
                combined_text += part['text']
            combined_images.extend(part['image_urls'])

        # 去重图片 URL
        seen = set()
        unique_images = []
        for url in combined_images:
            if url not in seen:
                seen.add(url)
                unique_images.append(url)

        # 构造最终回复
        final_reply = combined_text
        for url in unique_images:
            if final_reply:
                final_reply += '\n\n'
            final_reply += f'![image]({url})'

        self._reply_buffer = []
        self._reply_timer = None

        fut = self._pending_future
        self._pending_future = None
        if fut and not fut.done():
            logger.info(f"合并回复完成: text_len={len(combined_text)}, images={len(unique_images)}")
            fut.set_result(final_reply)

    async def send_and_wait(self, user_msg: str) -> str:
        """发送消息到群 @元宝，等待回复"""
        if self._pending_future is not None:
            raise Exception("已有待处理的请求")

        # 清理任何残留的 timer 和 buffer（理论上不应有，但防御性清理）
        if self._reply_timer:
            self._reply_timer.cancel()
            self._reply_timer = None
        self._reply_buffer = []

        future = asyncio.get_running_loop().create_future()
        self._pending_future = future

        # 发送 @元宝 消息
        full = user_msg
        await self.client.send_group_message(GROUP_CODE, full,
            at_user_id=YUANBAO_USER_ID, at_nickname=YUANBAO_NICK)
        logger.info(f"已发送请求: {user_msg[:60]}")

        try:
            reply = await asyncio.wait_for(future, timeout=120)
            return reply
        except asyncio.TimeoutError:
            self._pending_future = None
            self._reply_buffer = []
            if self._reply_timer:
                self._reply_timer.cancel()
                self._reply_timer = None
            raise TimeoutError("等待元宝回复超时")

    # ── HTTP 服务器 ──

    @staticmethod
    def _parse_http_request(data: bytes) -> dict | None:
        """解析 HTTP 请求，返回 {method, path, headers, body}"""
        try:
            # 分割 header 和 body
            parts = data.split(b'\r\n\r\n', 1)
            if len(parts) < 2:
                return None
            header_part, body = parts

            lines = header_part.decode('utf-8', errors='replace').split('\r\n')
            if not lines:
                return None

            first = lines[0].split(' ')
            if len(first) < 2:
                return None
            method, path = first[0], first[1]

            headers = {}
            for line in lines[1:]:
                if ':' in line:
                    k, v = line.split(':', 1)
                    headers[k.strip().lower()] = v.strip()

            return {'method': method, 'path': path, 'headers': headers, 'body': body}
        except Exception:
            return None

    @staticmethod
    def _http_response(code: int, msg: str, body: dict) -> bytes:
        """构造 HTTP 响应"""
        body_bytes = json.dumps(body, ensure_ascii=False).encode('utf-8')
        status = {200: 'OK', 400: 'Bad Request', 401: 'Unauthorized',
                  404: 'Not Found', 500: 'Internal Server Error', 503: 'Service Unavailable'}
        resp = (
            f"HTTP/1.1 {code} {status.get(code, 'Unknown')}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode('utf-8') + body_bytes
        return resp

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """处理单个 HTTP 客户端连接"""
        peer = writer.get_extra_info('peername')
        try:
            # 读取完整请求 (最多 64KB)
            data = b''
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                data += chunk
                # 检查是否收到完整 HTTP 请求
                if b'\r\n\r\n' in data:
                    # 解析头部获取 Content-Length
                    header_end = data.index(b'\r\n\r\n') + 4
                    headers_raw = data[:header_end].decode('utf-8', errors='replace')
                    cl = 0
                    for line in headers_raw.split('\r\n'):
                        if line.lower().startswith('content-length:'):
                            cl = int(line.split(':', 1)[1].strip())
                            break
                    # 如果 body 足够，则完整
                    if len(data) >= header_end + cl:
                        break
                    # 否则继续读
                    if len(data) > 102400:  # 100KB 上限
                        break

            req = self._parse_http_request(data)
            if not req:
                await writer.drain()
                writer.close()
                return

            logger.info(f"HTTP {req['method']} {req['path']} from {peer}")

            # ── 解析 Cookie ──
            cookie_str = req.get('headers', {}).get('cookie', '')
            cookies = self._parse_cookies(cookie_str)

            # ── 管理员路由（不需要 API Key） ──
            path = req['path']
            method = req['method']

            # GET / - 首页
            if path == '/' and method == 'GET':
                html = self._read_template('index.html')
                writer.write(self._html_response(200, html))
                await writer.drain()
                writer.close()
                return

            # GET /admin - 登录页面
            if path == '/admin' and method == 'GET':
                if self._verify_session(cookies):
                    writer.write(self._html_response(302, '', 'Location: /admin/panel\r\n'))
                else:
                    writer.write(self._html_response(200, self._render_admin_page('login')))
                await writer.drain()
                writer.close()
                return

            # POST /admin/login - 登录
            if path == '/admin/login' and method == 'POST':
                # 解析 form-urlencoded 或 JSON
                body_str = req['body'].decode('utf-8', errors='replace')
                form_data = {}
                if body_str.startswith('{'):
                    try:
                        form_data = json.loads(body_str)
                    except json.JSONDecodeError:
                        pass
                else:
                    for part in body_str.split('&'):
                        if '=' in part:
                            k, v = part.split('=', 1)
                            from urllib.parse import unquote_plus
                            form_data[unquote_plus(k)] = unquote_plus(v)
                username = form_data.get('username', '')
                password = form_data.get('password', '')
                if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                    token = self._generate_session()
                    writer.write(self._html_response(302, '',
                        f'Set-Cookie: yb_admin_token={token}; Path=/; HttpOnly; SameSite=Lax\r\n'
                        f'Location: /admin/panel\r\n'))
                else:
                    writer.write(self._html_response(200, self._render_admin_page('login', error='用户名或密码错误')))
                await writer.drain()
                writer.close()
                return

            # GET /admin/logout - 退出
            if path == '/admin/logout' and method == 'GET':
                token = cookies.get('yb_admin_token', '')
                if token in self._sessions:
                    del self._sessions[token]
                writer.write(self._html_response(302, '',
                    'Set-Cookie: yb_admin_token=; Path=/; Max-Age=0\r\n'
                    'Location: /admin\r\n'))
                await writer.drain()
                writer.close()
                return

            # 以下路由需要管理员会话
            admin_paths = ['/admin/panel', '/admin/api/config', '/admin/api/chat', '/admin/api/restart']
            is_admin = any(path.startswith(p) for p in ['/admin/'])
            if is_admin:
                if not self._verify_session(cookies):
                    writer.write(self._html_response(302, '', 'Location: /admin\r\n'))
                    await writer.drain()
                    writer.close()
                    return

                # GET /admin/panel - 管理面板
                if path == '/admin/panel' and method == 'GET':
                    writer.write(self._html_response(200,
                        self._render_admin_page('panel', connected=self.client.connected)))
                    await writer.drain()
                    writer.close()
                    return

                # GET /admin/api/config - 获取配置
                if path == '/admin/api/config' and method == 'GET':
                    cfg = _reload_config()
                    # 不返回密码原文（但允许管理员界面显示占位）
                    cfg_copy = {k: v for k, v in cfg.items()}
                    if 'ADMIN_PASSWORD' in cfg_copy:
                        cfg_copy['ADMIN_PASSWORD'] = ''  # 不回传密码
                    resp = self._http_response(200, 'OK', cfg_copy)
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

                # POST /admin/api/config - 保存配置
                if path == '/admin/api/config' and method == 'POST':
                    try:
                        new_cfg = json.loads(req['body'].decode('utf-8'))
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        resp = self._http_response(400, 'Bad Request',
                                                   {'error': f'Invalid JSON: {e}'})
                        writer.write(resp)
                        await writer.drain()
                        writer.close()
                        return
                    # 读取当前配置，合入新值
                    current = _reload_config()
                    allowed_keys = ['APP_ID', 'APP_SECRET', 'GROUP_CODE', 'YUANBAO_USER_ID',
                                    'YUANBAO_NICK', 'PORT', 'API_KEY', 'API_DOMAIN',
                                    'WS_URL', 'debug', 'ADMIN_USERNAME']
                    for k in allowed_keys:
                        if k in new_cfg:
                            current[k] = new_cfg[k]
                    # 密码特殊处理：只有非空才更新
                    if new_cfg.get('ADMIN_PASSWORD', '').strip():
                        current['ADMIN_PASSWORD'] = new_cfg['ADMIN_PASSWORD'].strip()
                    # 写入 config.json
                    try:
                        config_path = os.path.join(os.path.dirname(__file__), 'config.json')
                        with open(config_path, 'w', encoding='utf-8') as f:
                            json.dump(current, f, ensure_ascii=False, indent=4)
                        # 重新加载全局配置
                        _reload_config()
                        resp = self._http_response(200, 'OK', {'status': 'ok', 'message': '配置已保存'})
                    except Exception as e:
                        resp = self._http_response(500, 'Error',
                                                   {'error': f'保存失败: {e}'})
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

                # POST /admin/api/chat - 聊天测试
                if path == '/admin/api/chat' and method == 'POST':
                    try:
                        chat_req = json.loads(req['body'].decode('utf-8'))
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        resp = self._http_response(400, 'Bad Request',
                                                   {'error': f'Invalid JSON: {e}'})
                        writer.write(resp)
                        await writer.drain()
                        writer.close()
                        return
                    if not self.client.connected:
                        resp = self._http_response(503, 'Unavailable',
                                                   {'error': 'Bot not connected'})
                        writer.write(resp)
                        await writer.drain()
                        writer.close()
                        return
                    try:
                        reply = await self.send_and_wait(chat_req.get('message', ''))
                        resp = self._http_response(200, 'OK', {'reply': reply})
                    except TimeoutError:
                        resp = self._http_response(504, 'Gateway Timeout',
                                                   {'error': '等待元宝回复超时'})
                    except Exception as e:
                        resp = self._http_response(500, 'Error', {'error': str(e)})
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

                # POST /admin/api/restart - 重启服务
                if path == '/admin/api/restart' and method == 'POST':
                    import subprocess
                    try:
                        script = os.path.abspath(__file__)
                        pid = os.getpid()
                        # 后台启动新进程
                        subprocess.Popen([sys.executable, script])
                        # 返回成功，然后退出当前进程
                        resp = self._http_response(200, 'OK',
                                                   {'status': 'ok', 'message': '服务正在重启'})
                        writer.write(resp)
                        await writer.drain()
                        writer.close()
                        # 关闭服务器
                        self._running = False
                        if self._http_server:
                            self._http_server.close()
                        os._exit(0)
                    except Exception as e:
                        resp = self._http_response(500, 'Error', {'error': str(e)})
                        writer.write(resp)
                        await writer.drain()
                        writer.close()
                        return

                # 未知管理路径
                resp = self._http_response(404, 'Not Found', {'error': 'Not Found'})
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            # ── API Key 鉴权 ──
            if API_KEY:
                auth_header = req.get('headers', {}).get('authorization', '')
                if not auth_header.startswith('Bearer ') or auth_header[7:] != API_KEY:
                    resp = self._http_response(401, 'Unauthorized',
                                               {'error': 'Invalid API key',
                                                'message': '请在 Authorization 头中提供有效的 Bearer API key'})
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

            # ── OpenAI 兼容路由 ──
            if path == '/v1/models' and method == 'GET':
                resp_body = {
                    'object': 'list',
                    'data': [
                        {
                            'id': 'yuanbao',
                            'object': 'model',
                            'created': int(time.time()),
                            'owned_by': 'yuanbao',
                            'permission': []
                        }
                    ]
                }
                resp = self._http_response(200, 'OK', resp_body)
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            # ── OpenAI 图片生成 ──
            if path == '/v1/images/generations' and method == 'POST':
                try:
                    req_body = json.loads(req['body'].decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    resp = self._http_response(400, 'Bad Request', {
                        'error': f'Invalid JSON: {e}',
                        'message': '请求体必须是有效的 JSON'
                    })
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

                prompt = req_body.get('prompt', '').strip()
                if not prompt:
                    resp = self._http_response(400, 'Bad Request', {
                        'error': 'prompt is required',
                        'message': '请提供 prompt 字段（描述要生成的图片）'
                    })
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

                # 解析图片比例（OpenAI 标准 size 参数，如 "1024x1024"）
                size_str = req_body.get('size', '').strip()
                if not size_str:
                    width, height = 512, 512
                else:
                    parts = re.split(r'[x×*]', size_str.lower())
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        width, height = int(parts[0]), int(parts[1])
                    else:
                        width, height = 512, 512

                # 直接 @元宝，不发文件
                user_msg = f"System:请生成一张图片，不要发多余文字说明，直接发送图片，图片比例 {width}x{height}\n{prompt}"
                logger.info(f"图片生成请求: prompt='{prompt[:80]}' size={width}x{height}")
                try:
                    reply = await self.send_and_wait(user_msg)
                except TimeoutError:
                    resp = self._http_response(504, 'Gateway Timeout', {
                        'error': '等待元宝回复超时'
                    })
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

                # 从回复中提取图片 URL（格式：![image](url)）
                raw_urls = re.findall(r'!\[image\]\(([^)]+)\)', reply)

                if not raw_urls:
                    resp = self._http_response(500, 'Error', {
                        'error': '元宝未生成图片',
                        'message': reply[:300] if reply else '无回复'
                    })
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    return

                # 将保护资源 URL 解析为 COS 预签名直链
                resolved_urls = []
                for url in raw_urls:
                    if 'resourceId=' in url:
                        resolved = self.client.resolve_image_url(url)
                        if resolved:
                            resolved_urls.append(resolved)
                        else:
                            # 解析失败时保留原始 URL
                            resolved_urls.append(url)
                    else:
                        resolved_urls.append(url)

                if resolved_urls:
                    logger.info(f"图片直链解析完成: {len(resolved_urls)}/{len(raw_urls)} 张成功")
                else:
                    logger.warning("所有图片 URL 解析失败，返回原始 URL")

                # 构造 OpenAI 兼容响应
                data_entries = [{'url': url} for url in resolved_urls]
                resp_body = {
                    'created': int(time.time()),
                    'data': data_entries
                }
                resp = self._http_response(200, 'OK', resp_body)
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            if path != '/v1/chat/completions' or method != 'POST':
                resp = self._http_response(404, 'Not Found', {'error': 'Not Found'})
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            # 解析 body
            try:
                req_body = json.loads(req['body'].decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                body_preview = req['body'][:200].decode('utf-8', errors='replace')
                resp = self._http_response(400, 'Bad Request', {
                    'error': f'Invalid JSON: {e}',
                    'detail': body_preview
                })
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            # ── 多轮对话处理 ──
            messages = req_body.get('messages', [])
            if not messages:
                resp = self._http_response(400, 'Bad Request', {'error': 'No messages'})
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            # 根据最后一条消息的 role 决定前缀
            last_msg = messages[-1] if messages else None
            if not last_msg:
                resp = self._http_response(400, 'Bad Request',
                                           {'error': 'No messages'})
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            last_role = last_msg.get('role', 'user')
            last_content = SimpleProtobufCodec.extract_content_text(
                last_msg.get('content')
            )
            if last_role == 'tool':
                last_line = f"Tool:{last_content}"
            else:
                last_line = f"User:{last_content}"

            # ── 从静态文件读取历史示例和工具调用示例 ──
            base_dir = os.path.dirname(os.path.abspath(__file__))
            history_file = os.path.join(base_dir, '历史.txt')
            tools_file = os.path.join(base_dir, '工具.txt')

            # 保持 has_tools 用于后续工具调用解析
            tools = req_body.get('tools', [])
            tool_choice = req_body.get('tool_choice', 'auto')
            has_tools = bool(tools and tool_choice != 'none')

            if not self.client.connected:
                resp = self._http_response(503, 'Unavailable',
                                           {'error': 'Bot not connected'})
                writer.write(resp)
                await writer.drain()
                writer.close()
                return

            # 发送文件到群聊（COS 上传 + TIMFileElem），让元宝读取
            try:
                # 动态生成历史记录文件（对话上下文）
                history_lines = []
                for msg in messages:
                    role = msg.get('role', 'unknown')
                    content = SimpleProtobufCodec.extract_content_text(msg.get('content', ''))
                    if content:
                        history_lines.append(f"{role}: {content}")
                history_content = '\n'.join(history_lines)
                with open(history_file, 'w', encoding='utf-8') as f:
                    f.write(history_content)
                logger.info(f"已生成 {history_file} ({len(history_content)} bytes)")

                # 生成工具定义文件（如果请求中包含 tools）
                tools_content = ''
                if tools:
                    lines = ['【工具调用请求】', '']
                    lines.append('请根据用户问题从可用工具中选择一个，并严格按以下 JSON 格式回复（只返回 JSON，不要包含其他任何内容）：')
                    lines.append('')
                    lines.append('可用工具定义：')
                    lines.append('')
                    lines.append(json.dumps(tools, ensure_ascii=False, indent=2))
                    lines.append('')
                    lines.append('请仅返回 JSON 格式：{"tool_calls": [{"id": "call_xxx", "type": "function", "function": {"name": "工具名", "arguments": {"参数名": "参数值"}}}]}')
                    tools_content = '\n'.join(lines)
                    with open(tools_file, 'w', encoding='utf-8') as f:
                        f.write(tools_content)
                    logger.info(f"已生成 {tools_file} ({len(tools_content)} bytes)")

                # 发送文件后删除
                if history_content:
                    if await self.client.send_file(history_file):
                        os.remove(history_file)
                        logger.info(f"已删除: {history_file}")
                    await asyncio.sleep(1)
                if tools_content:
                    if await self.client.send_file(tools_file):
                        os.remove(tools_file)
                        logger.info(f"已删除: {tools_file}")
                    await asyncio.sleep(1)

                # @元宝 发送 System/User 格式消息
                if has_tools:
                    user_msg = (f"System:请读取历史.txt和工具.txt（有哪个读哪个），直接回答用户问题，无需告知我已读取。"
                                f"注意：不要使用系统内置工具，并且不要说系统没内置什么什么工具。"
                                f"请根据工具.txt返回对应的 JSON，稍后我会给出工具返回值。\n{last_line}")
                else:
                    user_msg = f"System:请读取历史.txt和工具.txt（有哪个读哪个），直接回答用户问题，无需告知我已读取\n{last_line}"
                reply = await self.send_and_wait(user_msg)

                # ── 尝试解析工具调用 JSON 响应（只有请求中包含 tools 时才解析）──
                tool_calls = None
                if has_tools:
                    try:
                        text = reply.strip()
                        # 如果被 ```json ... ``` 包裹，提取中间内容
                        if text.startswith('```'):
                            lines = text.split('\n')
                            cleaned = []
                            in_code = False
                            for line in lines:
                                if line.strip().startswith('```'):
                                    in_code = not in_code
                                    continue
                                if in_code:
                                    cleaned.append(line)
                            text = '\n'.join(cleaned).strip()

                        parsed = json.loads(text)
                        if isinstance(parsed, dict) and 'tool_calls' in parsed:
                            raw_calls = parsed['tool_calls']
                            if isinstance(raw_calls, list) and len(raw_calls) > 0:
                                valid_calls = []
                                for tc in raw_calls:
                                    if (isinstance(tc, dict)
                                            and tc.get('type') == 'function'
                                            and tc.get('function', {}).get('name')):
                                        func = tc['function']
                                        args = func.get('arguments', {})
                                        args_str = (json.dumps(args, ensure_ascii=False)
                                                    if isinstance(args, dict) else str(args))
                                        valid_calls.append({
                                            'id': tc.get('id', f'call_{uuid.uuid4().hex[:8]}'),
                                            'type': 'function',
                                            'function': {
                                                'name': func['name'],
                                                'arguments': args_str
                                            }
                                        })
                                if valid_calls:
                                    tool_calls = valid_calls
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass

                if tool_calls:
                    # 工具调用：返回 tool_calls 格式
                    resp_body = {
                        'id': f'chatcmpl-{uuid.uuid4().hex}',
                        'object': 'chat.completion',
                        'created': int(time.time()),
                        'model': 'yuanbao',
                        'choices': [{
                            'index': 0,
                            'message': {
                                'role': 'assistant',
                                'content': None,
                                'tool_calls': tool_calls
                            },
                            'finish_reason': 'tool_calls'
                        }],
                        'usage': {
                            'prompt_tokens': len(user_msg),
                            'completion_tokens': len(reply),
                            'total_tokens': len(user_msg) + len(reply)
                        }
                    }
                    logger.info(f"工具调用成功: "
                                f"{json.dumps(tool_calls, ensure_ascii=False)[:100]}")
                else:
                    # 普通文本回复
                    resp_body = {
                        'id': f'chatcmpl-{uuid.uuid4().hex}',
                        'object': 'chat.completion',
                        'created': int(time.time()),
                        'model': 'yuanbao',
                        'choices': [{
                            'index': 0,
                            'message': {
                                'role': 'assistant',
                                'content': reply
                            },
                            'finish_reason': 'stop'
                        }],
                        'usage': {
                            'prompt_tokens': len(user_msg),
                            'completion_tokens': len(reply),
                            'total_tokens': len(user_msg) + len(reply)
                        }
                    }
                    logger.info(f"回复成功: {reply[:60]}")

                resp = self._http_response(200, 'OK', resp_body)
                writer.write(resp)
            except TimeoutError:
                resp = self._http_response(504, 'Gateway Timeout',
                                           {'error': '元宝回复超时'})
                writer.write(resp)
            except Exception as e:
                logger.error(f"处理请求异常: {e}")
                resp = self._http_response(500, 'Error', {'error': str(e)})
                writer.write(resp)

            await writer.drain()

        except Exception as e:
            logger.error(f"HTTP 处理异常: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def run(self):
        """启动守护进程"""
        # 1. 连接 WebSocket
        await self.client.connect()
        self._connected.set()
        logger.info(f"WebSocket 已连接，群组: {GROUP_CODE}")

        # 2. 启动心跳
        asyncio.create_task(self._heartbeat_loop())

        # 3. 启动接收循环
        asyncio.create_task(self._receive_loop())

        # 4. 等待片刻后发送测试消息，确认元宝在线后再启动 HTTP 服务器
        await asyncio.sleep(3)
        try:
            test_reply = await self.send_and_wait("系统上线测试")
            logger.info(f"元宝已就绪: {test_reply[:60]}")
        except Exception as e:
            logger.warning(f"元宝未响应，仍继续启动: {e}")

        # 5. 启动 HTTP 服务器
        self._http_server = await asyncio.start_server(
            self._handle_client, '0.0.0.0', SERVER_PORT
        )
        addr = self._http_server.sockets[0].getsockname()
        logger.info(f"HTTP 服务器已启动: http://{addr[0]}:{addr[1]}")
        logger.info(f"OpenAI API 端点: POST http://localhost:{SERVER_PORT}/v1/chat/completions")

        # 保持运行
        try:
            async with self._http_server:
                await self._http_server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await self.cleanup()

    async def cleanup(self):
        self._running = False
        if self._pending_future and not self._pending_future.done():
            self._pending_future.cancel()
        await self.client.disconnect()


def main():
    import sys
    logger.info("=" * 50)
    logger.info("元宝 Bot 守护进程启动")
    logger.info(f"目标群: {GROUP_CODE}")
    logger.info(f"元宝ID: {YUANBAO_USER_ID}")
    logger.info(f"监听端口: {SERVER_PORT}")
    logger.info("=" * 50)

    daemon = YBDaemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
    except Exception as e:
        logger.error(f"程序异常: {e}")
        raise


if __name__ == '__main__':
    main()
