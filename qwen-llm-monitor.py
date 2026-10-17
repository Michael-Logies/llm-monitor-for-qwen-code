#!/usr/bin/env python3
"""LLM-Monitor für Qwen Code (v3, 17.10.2026) — GTK3-Desktop-Badge.

Zeigt in drei Zeilen, was die laufenden Qwen-Code-Sessions wirklich tun:

1. Zeile 1 (aus dem OTel-Datenstrom): Modell, das tatsächlich geantwortet hat
   (gen_ai.response.model) · Anbieter-Host aus der echten Request-URL (url.full)
   · HTTP-Status. Vor der ersten Anfrage: die konfigurierte Route aus der
   settings.json, klar als „konfiguriert" markiert.
2. Zeile 2 (OTel-Datenstrom): Denkphase (TTFB) · tok/s · Antwort-Tokens ·
   gelesene Tokens (davon Cache) · Dauer.
3. Zeile 3: Alter der letzten Messung · Max-Kontextfenster · unterstützte
   Eingaben · Alter der letzten Konfig-Änderung.

Datenquelle ist ein eingebetteter OTLP/HTTP-Empfänger (Standard-Port 4318, nur
Loopback). Qwen Code liefert mit `otlpProtocol: "http"` OTLP/JSON (chunked,
application/json); ein Protobuf-Pfad bleibt für gRPC-Sender erhalten.

Sprachen: Deutsch/Englisch/Chinesisch — gewählt über die LANG-Umgebung
(de* → Deutsch, zh* → Chinesisch, sonst Englisch als Default).

Konfiguration über Umgebungsvariablen:
  QWEN_MONITOR_SETTINGS  Pfad zur settings.json (Default ~/.qwen/settings.json)
  QWEN_MONITOR_OTLP_PORT OTLP-Empfangsport (Default 4318)
  QWEN_MONITOR_WIDTH     Badge-Breite in Pixeln (Default 600)

Bedienung: Verschieben mit gedrückter linker Maustaste (Position in
~/.qwen/route-widget-pos.json), Rechtsklick → Beenden, Einzelinstanz via
Loopback-Socket 127.0.0.1:8788, Autostart über ~/.config/autostart.
"""
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk

SETTINGS_FILE = os.environ.get('QWEN_MONITOR_SETTINGS',
                               os.path.expanduser('~/.qwen/settings.json'))
POS_FILE = os.path.expanduser('~/.qwen/route-widget-pos.json')
OTLP_HOST = '127.0.0.1'
OTLP_PORT = int(os.environ.get('QWEN_MONITOR_OTLP_PORT', '4318'))
POLL_MS = 3000
MARGIN = 24
WIDTH = int(os.environ.get('QWEN_MONITOR_WIDTH', '600'))

# Anzeige-Kürzel für Modell-IDs (unbekannte erscheinen roh).
MODEL_LABEL = {
    'openrouter/auto': 'Auto-Router',
    'z-ai/glm-5.3-flash': 'GLM-5.3-Flash',
    'glm-5.3-flash': 'GLM-5.3-Flash',
    'glm-5.3-flash-proxy': 'GLM-5.3-Flash',
    'xiaomi/mimo-v2.6-pro': 'MiMo-V2.6-Pro',
    'anthropic/claude-sonnet-5': 'Claude-Sonnet-5',
    'deepseek-v4-flash': 'DeepSeek-V4-Flash',
}

COLOR_ZAI = '#7ce38b'
COLOR_OR = '#8ec7ff'
COLOR_PROXY = '#ffb454'
COLOR_MS = '#c792ea'
COLOR_ERR = '#ff8a80'
COLOR_DIM = '#9aa0a6'

# --- i18n: de/zh/en — gewählt über LANG (de* → de, zh* → zh, sonst en) ---
_LANG = (os.environ.get('LANG') or '').lower()
LANG = 'de' if _LANG.startswith('de') else ('zh' if _LANG.startswith('zh') else 'en')

L10N = {
    'de': {
        'thinking': 'Denkphase (TTFB)', 'reply': 'Antwort', 'read': 'gelesen',
        'from_cache': 'aus Cache', 'duration': 'Dauer', 'tokps': 'tok/s',
        'last_req': 'letzte Anfrage %s', 'max_ctx': 'Max-Kontext',
        'reads': 'liest', 'config_changed': 'Konfig geändert %s',
        'no_data': 'Telemetrie: noch keine Daten (%d) — neue Qwen-Session nötig?',
        'no_attrs': 'Telemetrie: keine Span-Attribute',
        'port_busy': 'OTLP-Port %d belegt — kein Empfang',
        'no_metrics': 'Span ohne Messwerte',
        'awaiting': 'warte auf erste Anfrage', 'configured': 'konfiguriert',
        'unknown': 'Route unbekannt', 'quit': 'Beenden',
        'err_generic': 'Fehler',
        'age_s': 'vor %d s', 'age_min': 'vor %d min', 'age_h': 'vor %.1f h',
    },
    'en': {
        'thinking': 'Thinking time (TTFB)', 'reply': 'Reply', 'read': 'read',
        'from_cache': 'from cache', 'duration': 'Duration', 'tokps': 'tok/s',
        'last_req': 'last request %s', 'max_ctx': 'Max context',
        'reads': 'reads', 'config_changed': 'config changed %s',
        'no_data': 'Telemetry: no data yet (%d) — new Qwen session needed?',
        'no_attrs': 'Telemetry: no span attributes',
        'port_busy': 'OTLP port %d in use — no receiver',
        'no_metrics': 'Span without metrics',
        'awaiting': 'waiting for first request', 'configured': 'configured',
        'unknown': 'unknown route', 'quit': 'Quit',
        'err_generic': 'error',
        'age_s': '%d s ago', 'age_min': '%d min ago', 'age_h': '%.1f h ago',
    },
    'zh': {
        'thinking': '思考阶段 (TTFB)', 'reply': '回答', 'read': '已读取',
        'from_cache': '来自缓存', 'duration': '耗时', 'tokps': 'tok/s',
        'last_req': '上次请求 %s', 'max_ctx': '最大上下文',
        'reads': '支持', 'config_changed': '配置 %s更改',
        'no_data': '遥测：暂无数据 (%d) — 需要新的 Qwen 会话？',
        'no_attrs': '遥测：无 Span 属性',
        'port_busy': 'OTLP 端口 %d 被占用 — 无法接收',
        'no_metrics': 'Span 无指标',
        'awaiting': '等待首次请求', 'configured': '已配置',
        'unknown': '未知路由', 'quit': '退出',
        'err_generic': '错误',
        'age_s': '%d 秒前', 'age_min': '%d 分钟前', 'age_h': '%.1f 小时前',
    },
}

MOD_NAMES = {
    'de': {'image': 'Bilder', 'video': 'Video', 'pdf': 'PDF'},
    'en': {'image': 'images', 'video': 'video', 'pdf': 'PDF'},
    'zh': {'image': '图片', 'video': '视频', 'pdf': 'PDF'},
}


def T(key):
    return L10N[LANG][key]


# --- JSONC-Loader (settings.json enthält // und /* */-Kommentare) ---
def load_jsonc(path):
    with open(path) as f:
        txt = f.read()
    out = []
    i, n = 0, len(txt)
    in_str = False
    while i < n:
        c = txt[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                out.append(txt[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == '/' and i + 1 < n and txt[i + 1] == '/':
            while i < n and txt[i] != '\n':
                i += 1
        elif c == '/' and i + 1 < n and txt[i + 1] == '*':
            i += 2
            while i + 1 < n and not (txt[i] == '*' and txt[i + 1] == '/'):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return json.loads(''.join(out))


# --- Minimaler Protobuf-Wire-Parser für OTLP-Traces ---
def _varint(buf, i):
    result = 0
    shift = 0
    while True:
        b = buf[i]
        result |= (b & 0x7F) << shift
        i += 1
        if not (b & 0x80):
            return result, i
        shift += 7


def _iter_fields(buf):
    i, n = 0, len(buf)
    while i < n:
        tag, i = _varint(buf, i)
        fno, wt = tag >> 3, tag & 7
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt == 1:
            val = bytes(buf[i:i + 8])
            i += 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            val = bytes(buf[i:i + ln])
            i += ln
        elif wt == 5:
            val = bytes(buf[i:i + 4])
            i += 4
        else:
            raise ValueError('wire type %d' % wt)
        yield fno, wt, val


def _any_value(buf):
    for fno, wt, v in _iter_fields(buf):
        if fno == 1:
            return v.decode('utf-8', 'replace')
        if fno == 2:
            return bool(v)
        if fno == 3:  # int64 — echtes OTLP: wire type 0 (v bereits int);
            # Toleranz: LEN-Wrapper (wt 2) wird als varint-Payload dekodiert.
            if wt == 0:
                return v
            try:
                val, _ = _varint(v, 0)
                return val
            except Exception:
                return None
        if fno == 4:
            return struct.unpack('<d', v)[0]
        if fno == 5:  # ArrayValue { repeated AnyValue values = 1; }
            return [_any_value(m) for f, w, m in _iter_fields(v) if f == 1]
        if fno == 6:  # KeyValueList
            d = {}
            for f, w, m in _iter_fields(v):
                if f == 1:
                    k, val = _key_value(m)
                    d[k] = val
            return d
        if fno == 7:
            return bytes(v)
    return None


def _key_value(buf):
    key, val = None, None
    for fno, wt, v in _iter_fields(buf):
        if fno == 1:
            key = v.decode('utf-8', 'replace')
        elif fno == 2:
            val = _any_value(v)
    return key, val


def _parse_span(buf):
    name, start_ns, end_ns, attrs, status = None, None, None, {}, 0
    for fno, wt, v in _iter_fields(buf):
        if fno == 4:
            name = v.decode('utf-8', 'replace')
        elif fno == 6 and wt == 1:
            start_ns = struct.unpack('<Q', v)[0]
        elif fno == 7 and wt == 1:
            end_ns = struct.unpack('<Q', v)[0]
        elif fno == 8:
            k, val = _key_value(v)
            if k is not None:
                attrs[k] = val
        elif fno == 15:  # Status { code = 1 } — 2 = ERROR
            for sf, sw, sv in _iter_fields(v):
                if sf == 1:
                    status = sv
    return {'name': name, 'start_ns': start_ns, 'end_ns': end_ns,
            'attrs': attrs, 'status': status}


def _span_from_json(sp):
    attrs = {}
    for kv in sp.get('attributes') or []:
        v = kv.get('value') or {}
        for vk in ('stringValue', 'intValue', 'boolValue', 'doubleValue', 'bytesValue'):
            if vk in v:
                val = v[vk]
                if vk == 'intValue':
                    try:
                        val = int(str(val))
                    except Exception:
                        pass
                attrs[kv.get('key')] = val
                break

    def _ns(x):
        try:
            return int(str(x)) if x is not None else None
        except Exception:
            return None

    return {'name': sp.get('name'), 'start_ns': _ns(sp.get('startTimeUnixNano')),
            'end_ns': _ns(sp.get('endTimeUnixNano')), 'attrs': attrs,
            'status': int((sp.get('status') or {}).get('code') or 0)}


def parse_otlp_traces(body):
    # otlpProtocol "http" liefert OTLP/JSON (chunked) — Protobuf-Pfad bleibt für grpc-Sender
    if body[:1] == b'{':
        spans = []
        try:
            doc = json.loads(body.decode('utf-8'))
            for rs in doc.get('resourceSpans') or []:
                for ss in rs.get('scopeSpans') or []:
                    for sp in ss.get('spans') or []:
                        spans.append(_span_from_json(sp))
        except Exception as e:
            sys.stderr.write('OTLP parse: %r\n' % e)
        return spans
    spans = []
    try:
        for f1, w1, res_spans in _iter_fields(body):
            if f1 != 1:  # ExportTraceServiceRequest.resource_spans
                continue
            for f2, w2, scope_spans in _iter_fields(res_spans):
                if f2 != 2:  # ResourceSpans.scope_spans
                    continue
                for f3, w3, span in _iter_fields(scope_spans):
                    if f3 == 2:  # ScopeSpans.spans
                        spans.append(_parse_span(span))
    except Exception as e:
        sys.stderr.write('OTLP parse: %r\n' % e)
    return spans


# --- Route-Auflösung aus settings.json ---
def resolve_route(cfg):
    model = cfg.get('model') or {}
    name = model.get('name')
    base = model.get('baseUrl') or ''
    providers = (cfg.get('modelProviders') or {}).get('openai') or []

    entry = None
    if name is not None:
        for p in providers:
            if p.get('id') != name:
                continue
            eb = p.get('baseUrl') or ''
            if not base or eb == base:
                entry = p
                break
        if entry is None:
            for p in providers:
                if p.get('id') == name:
                    entry = p
                    break

    def is_zai(e, b):
        return 'api.z.ai' in b or (e or {}).get('envKey') == 'ZAI_API_KEY'

    def is_or(e, b):
        return 'openrouter.ai' in b or (e or {}).get('envKey') == 'OPENROUTER_API_KEY'

    def is_proxy(b):
        return b.startswith('http://127.0.0.1:8787')

    def is_ms(e, b):
        return 'dashscope' in b or (e or {}).get('envKey') == 'DASHSCOPE_API_KEY'

    ebase = (entry or {}).get('baseUrl') or ''
    if is_proxy(base) or is_proxy(ebase):
        label, color = 'Proxy', COLOR_PROXY
    elif is_zai(entry, base or ebase):
        label, color = 'Z.ai Coding Plan', COLOR_ZAI
    elif is_or(entry, base or ebase):
        label, color = 'OpenRouter', COLOR_OR
    elif is_ms(entry, base or ebase):
        label, color = 'ModelStudio', COLOR_MS
    else:
        label, color = (entry or {}).get('name') or name or 'Route unbekannt', '#e8eaed'

    mid = (entry or {}).get('id') or name
    model_disp = MODEL_LABEL.get(mid, mid)
    effort = model.get('reasoningEffort') or \
        ((entry or {}).get('generationConfig') or {}).get('reasoning', {}).get('effort')
    gen = (entry or {}).get('generationConfig') or {}
    ctx = gen.get('contextWindowSize')
    mods = [k for k in ('image', 'video', 'pdf') if (gen.get('modalities') or {}).get(k)]
    return {
        'label': label, 'color': color, 'model': model_disp, 'effort': effort,
        'ctx': ctx, 'mods': mods, 'proxy': is_proxy(base) or is_proxy(ebase),
        'entry': entry,
    }


def _sep():
    return ',' if LANG == 'de' else '.'


def fmt_ms(ms):
    if ms is None:
        return '–'
    s = ms / 1000.0
    if s < 10:
        return ('%.1f s' % s).replace('.', _sep())
    return ('%.0f s' % s).replace('.0', '')


def fmt_num(x):
    return ('%.1f' % x).replace('.', _sep())


def fmt_int(x):
    s = '{:,}'.format(int(x))
    return s.replace(',', '.') if LANG == 'de' else s


def fmt_compact(x):
    if x >= 1000000:  # dezimale Schwelle — Z.ai/ModelStudio nutzen runde 1M-Kontexte
        return fmt_num(x / 1000000.0) + 'M'
    if x >= 1000:
        return fmt_num(x / 1000.0) + 'k'
    return str(int(x))


def fmt_mio(x):
    if x >= 1000000:
        if LANG == 'de':
            return '%s Mio' % fmt_num(x / 1000000.0)
        return fmt_num(x / 1000000.0) + 'M'
    return fmt_compact(x)


def fmt_age(sec):
    if sec < 90:
        return T('age_s') % sec
    if sec < 5400:
        return T('age_min') % (sec // 60)
    out = T('age_h') % (sec / 3600.0)
    return out.replace('.', _sep()) if LANG == 'de' else out


class OtlpState:
    """Empfangene LLM-Spans (Thread-sicher, letzter gewinnt)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last = None       # dict je LLM-Span
        self.received = 0
        self.http = None       # letzter HTTP-Client-Span (url_full, http_status)

    def set_http(self, attrs):
        info = {'url_full': attrs.get('url.full') or '',
                'http_status': attrs.get('http.response.status_code')}
        with self.lock:
            self.http = info

    def http_snapshot(self):
        with self.lock:
            return dict(self.http) if self.http else None

    def add(self, span):
        a = span['attrs']
        ttfb_s = a.get('gen_ai.response.time_to_first_chunk')
        ttfb_ms = a.get('ttft_ms')
        if ttfb_ms is None and ttfb_s is not None:
            ttfb_ms = float(ttfb_s) * 1000.0
        out = a.get('gen_ai.usage.output_tokens')
        dur_s = None
        if span['start_ns'] and span['end_ns']:
            dur_s = (span['end_ns'] - span['start_ns']) / 1e9
        tps = a.get('output_tokens_per_second')
        if tps is None and out and dur_s and dur_s >= 0.5:
            tps = out / dur_s
        rec = {
            'at': time.time(),
            'model': a.get('gen_ai.response.model') or a.get('gen_ai.request.model'),
            'ttfb_ms': ttfb_ms,
            'tps': tps,
            'in_tok': a.get('gen_ai.usage.input_tokens'),
            'cache_tok': a.get('gen_ai.usage.cache_read.input_tokens'),
            'out_tok': out,
            'dur_s': dur_s,
            'url_full': a.get('url.full') or '',
            'http_status': a.get('http.response.status_code'),
            'error': a.get('error.type') or a.get('errorStatusCode'),
            'is_error': span['status'] == 2,
        }
        with self.lock:
            self.last = rec
            self.received += 1
        sys.stderr.write('SPAN model=%s ttfb=%s tps=%s out=%s in=%s err=%s\n'
                         % (rec['model'], rec['ttfb_ms'], rec['tps'],
                            rec['out_tok'], rec['in_tok'], rec['error']))
        sys.stderr.flush()

    def snapshot(self):
        with self.lock:
            return dict(self.last) if self.last else None, self.received


STATE = OtlpState()


class OtlpHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            ct = self.headers.get('Content-Type') or '-'
            te = self.headers.get('Transfer-Encoding') or '-'
            cl = self.headers.get('Content-Length') or '-'
            if 'chunked' in te.lower():
                chunks = []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        break
                    size = int(line.split(b';')[0].strip(), 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()
                body = b''.join(chunks)
            else:
                n = int(cl) if cl.isdigit() else 0
                body = self.rfile.read(n) if n else b''
            if '/v1/traces' in (self.path or ''):
                sys.stderr.write('RECV %s ct=%s te=%s clen=%s got=%d head=%s\n'
                                 % (self.path, ct, te, cl, len(body), body[:32].hex()))
                sys.stderr.flush()
                try:
                    spans = list(parse_otlp_traces(body))
                except Exception as pe:
                    sys.stderr.write('parse: %r\n' % pe)
                    spans = []
                for span in spans:
                    if 'gen_ai.request.model' in span['attrs']:
                        STATE.add(span)
                    elif 'url.full' in span['attrs']:
                        STATE.set_http(span['attrs'])
        except Exception as e:
            sys.stderr.write('OTLP handler: %r\n' % e)
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-protobuf')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *_a):
        pass


class App:
    def __init__(self):
        # Einzelinstanz: zweiter Start beendet sich still.
        self.lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.lock.bind(('127.0.0.1', 8788))
            self.lock.listen(1)
        except OSError:
            sys.exit(0)

        self.win = Gtk.Window()
        self.win.set_title('Route-Widget')
        self.win.set_decorated(False)
        self.win.set_keep_above(True)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_accept_focus(False)
        self.win.set_resizable(False)
        self.win.set_size_request(WIDTH, -1)

        css = b"""
        .badge { background: #16181e; border: 1px solid #454a54;
                 border-radius: 9px; padding: 9px 13px; }
        .badge label { color: #e8eaed; }
        .route { font-weight: bold; font-size: 11pt; }
        .metrics { font-size: 9pt; }
        .context { font-size: 8pt; color: #c3c8d0; }
        """
        prov = Gtk.CssProvider()
        prov.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.win.add(outer)
        self.badge = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.badge.get_style_context().add_class('badge')
        outer.pack_start(self.badge, True, True, 0)

        self.lbl_route = Gtk.Label()
        self.lbl_route.get_style_context().add_class('route')
        self.lbl_route.set_xalign(0)
        self.lbl_metrics = Gtk.Label()
        self.lbl_metrics.get_style_context().add_class('metrics')
        self.lbl_metrics.set_xalign(0)
        self.lbl_ctx = Gtk.Label()
        self.lbl_ctx.get_style_context().add_class('context')
        self.lbl_ctx.set_xalign(0)
        self.badge.pack_start(self.lbl_route, False, False, 0)
        self.badge.pack_start(self.lbl_metrics, False, False, 0)
        self.badge.pack_start(self.lbl_ctx, False, False, 0)

        self.route = None            # resolve_route-Ergebnis (Konfig-Fallback Zeile 1)
        self.settings_mtime = None
        self.settings_age = None     # Sekunden seit letzter settings.json-Änderung
        self.otlp_ok = True

        self.win.connect('button-press-event', self.on_press)
        self.win.connect('configure-event', self.on_configure)
        self.win.connect('destroy', Gtk.main_quit)

        self.place()
        self.win.show_all()
        self.render()
        self.start_otlp()
        GLib.timeout_add(POLL_MS, self.poll)
        self.poll()

    # --- OTLP-Empfänger ---
    def start_otlp(self):
        try:
            srv = ThreadingHTTPServer((OTLP_HOST, OTLP_PORT), OtlpHandler)
            srv.daemon_threads = True
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            self.otlp_ok = True
        except OSError as e:
            self.otlp_ok = False
            sys.stderr.write('OTLP-Server nicht gestartet: %r\n' % e)

    # --- Positionierung (wie v1) ---
    def place(self):
        pos = {}
        try:
            with open(POS_FILE) as f:
                pos = json.load(f)
        except Exception:
            pos = {}
        display = Gdk.Display.get_default()
        mon = display.get_monitor_at_window(self.win.get_window()) \
            if self.win.get_window() else display.get_primary_monitor()
        if mon is None:
            mon = display.get_monitor(0)
        geom = mon.get_geometry()
        if 'x' in pos and 'y' in pos:
            x, y = int(pos['x']), int(pos['y'])
            x = max(0, min(x, geom.x + geom.width - WIDTH))
            y = max(0, min(y, geom.y + geom.height - 40))
        else:
            x = geom.x + MARGIN
            y = geom.y + geom.height - 80 - MARGIN
        self.win.move(x, y)

    def on_configure(self, _w, ev):
        if getattr(self, '_last_pos', None) == (ev.x, ev.y):
            return False
        self._last_pos = (ev.x, ev.y)
        try:
            with open(POS_FILE, 'w') as f:
                json.dump({'x': int(ev.x), 'y': int(ev.y)}, f)
        except Exception:
            pass
        return False

    # --- Interaktion ---
    def on_press(self, _w, ev):
        if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 1:
            self.win.begin_move_drag(int(ev.button), int(ev.x_root), int(ev.y_root), ev.time)
        elif ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 3:
            menu = Gtk.Menu()
            mi = Gtk.MenuItem(label=T('quit'))
            mi.connect('activate', lambda *_: Gtk.main_quit())
            menu.append(mi)
            menu.show_all()
            menu.popup_at_pointer(ev)

    # --- Daten + Anzeige ---
    def poll(self):
        try:
            st = os.stat(SETTINGS_FILE)
            mtime = st.st_mtime
            if mtime != self.settings_mtime:
                if self.settings_mtime is not None:
                    self.settings_age = 0.0  # gerade geändert → Alter neu zählen
                else:
                    self.settings_age = max(0.0, time.time() - mtime)
                self.settings_mtime = mtime
                self.settings_loaded_at = time.time()
                try:
                    self.route = resolve_route(load_jsonc(SETTINGS_FILE))
                except Exception as e:
                    sys.stderr.write('settings parse: %r\n' % e)
            if self.settings_age is not None:
                self.settings_age += POLL_MS / 1000.0
        except Exception as e:
            sys.stderr.write('poll: %r\n' % e)
        self.render()
        return True

    @staticmethod
    def esc(s):
        return (str(s).replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;'))

    @staticmethod
    def set_markup(label, markup):
        label.set_markup(markup or ' ')

    def render(self):
        r = self.route
        last, _total = STATE.snapshot()
        http = STATE.http_snapshot() or {}
        # Zeile 1: was wirklich zurückkam (Datenstrom); vorher Konfig-Anzeige
        if last:
            url = str(http.get('url_full') or '')
            status = http.get('http_status')
            if 'z.ai' in url:
                color = COLOR_ZAI
            elif 'openrouter' in url:
                color = COLOR_OR
            elif '127.0.0.1' in url or 'localhost' in url:
                color = COLOR_PROXY
            elif 'dashscope' in url:
                color = COLOR_MS
            else:
                color = '#e8eaed'
            parts = ['<span foreground="%s">%s</span>'
                     % (color, self.esc(last.get('model') or T('unknown')))]
            if '://' in url:
                try:
                    host = urllib.parse.urlsplit(url).hostname
                except Exception:
                    host = None
                if host:
                    parts.append(self.esc(host))
            if status is not None:
                parts.append('HTTP %s' % self.esc(status))
            line1 = ' · '.join(parts)
        elif r:
            cfg = ' · '.join(str(x) for x in (r['label'], r.get('model'),
                                              r.get('effort')) if x)
            line1 = ('<span foreground="%s">%s:</span> %s'
                     % (COLOR_DIM, T('configured'), self.esc(cfg)))
        else:
            line1 = '<span foreground="%s">Qwen Code — %s</span>' % (COLOR_DIM, T('awaiting'))
        self.set_markup(self.lbl_route, line1)

        # Zeile 2: Verkehrsdaten aus dem letzten OTel-Span.
        line2, line3ctx = self.render_telemetry()
        self.set_markup(self.lbl_metrics, line2)

        # Zeile 3: Alter + Kontext
        ctx = list(line3ctx)
        if r:
            if r.get('ctx'):
                ctx.append('%s %s' % (T('max_ctx'), fmt_mio(r['ctx'])))
            if r.get('mods'):
                ctx.append('%s %s' % (T('reads'), ', '.join(
                    MOD_NAMES[LANG].get(k, k) for k in r['mods'])))
        if self.settings_age is not None:
            ctx.append(T('config_changed') % fmt_age(int(self.settings_age)))
        self.set_markup(self.lbl_ctx, ' · '.join(ctx) or ' ')

    def render_telemetry(self):
        last, total = STATE.snapshot()
        ctx = []
        if not self.otlp_ok:
            return ('<span foreground="%s">%s</span>'
                    % (COLOR_ERR, self.esc(T('port_busy') % OTLP_PORT))), ctx
        if last is None:
            note = (T('no_data') % total) if total == 0 else T('no_attrs')
            return '<span foreground="%s">%s</span>' % (COLOR_DIM, self.esc(note)), ctx

        if last.get('is_error') or last.get('error'):
            err = last.get('error') or T('err_generic')
            line2 = '<span foreground="%s">%s</span>' % (COLOR_ERR, self.esc(err))
        else:
            parts = []
            if last.get('ttfb_ms') is not None:
                parts.append('%s %s' % (T('thinking'), fmt_ms(last['ttfb_ms'])))
            if last.get('tps') and 0 < last['tps'] <= 300:
                parts.append('%s %s' % (fmt_num(last['tps']), T('tokps')))
            if last.get('out_tok') is not None:
                parts.append('%s %s T' % (T('reply'), fmt_int(last['out_tok'])))
            ins = ''
            if last.get('in_tok') is not None:
                ins = '%s %s T' % (T('read'), fmt_int(last['in_tok']))
                if last.get('cache_tok'):
                    ins += ' (%s %s)' % (fmt_int(last['cache_tok']), T('from_cache'))
            if ins:
                parts.append(ins)
            if last.get('dur_s'):
                parts.append('%s %s s' % (T('duration'), fmt_num(last['dur_s'])))
            line2 = ' · '.join(parts) or T('no_metrics')
        ctx.append(T('last_req') % fmt_age(max(0, int(time.time() - last['at']))))
        return line2, ctx

def main():
    App()
    Gtk.main()


if __name__ == '__main__':
    main()
