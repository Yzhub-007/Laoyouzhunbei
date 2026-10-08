"""Loopback-only OpenAI-compatible adapter for V-FloodNet image segmentation.

The main application already sends privacy-processed image data to a configured
VISION_ENDPOINT. This sidecar accepts that request locally, runs the upstream
V-FloodNet image segmentation checkpoint, and returns the strict JSON verdict
expected by camera_vision.py. It does not estimate water depth or save images.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_REQUEST_BYTES = 6 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MODEL_NAME = 'v-floodnet-image-segmentation'
SUPPORTED_DATA_TYPES = {'image/jpeg', 'image/png'}


class RequestError(ValueError):
    """A client request failed strict local validation."""


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def extract_image_data(payload):
    """Return the one JPEG/PNG data URL embedded in an OpenAI-style request."""
    if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list):
        raise RequestError('请求必须包含messages数组。')
    candidates = []
    for message in payload['messages']:
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get('type') != 'image_url':
                continue
            image_url = item.get('image_url')
            if isinstance(image_url, dict) and isinstance(image_url.get('url'), str):
                candidates.append(image_url['url'])
    if len(candidates) != 1:
        raise RequestError('请求必须且只能包含一张图片。')
    url = candidates[0]
    if not url.startswith('data:') or ';base64,' not in url:
        raise RequestError('只接受本地内嵌的JPEG/PNG图片。')
    header, encoded = url.split(',', 1)
    media_type = header[5:].split(';', 1)[0].lower()
    if media_type not in SUPPORTED_DATA_TYPES:
        raise RequestError('图片必须为JPEG或PNG。')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise RequestError('图片base64无效。') from None
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise RequestError('图片为空或超过5 MiB。')
    return data, media_type


def chat_response(result, model_name=DEFAULT_MODEL_NAME):
    content = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    return {
        'id': 'vfloodnet-local',
        'object': 'chat.completion',
        'model': model_name,
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content},
                     'finish_reason': 'stop'}],
    }


class VFloodNetEngine:
    def __init__(self, checkpoint, *, device='auto', mask_threshold=0.5,
                 min_water_fraction=0.002):
        self.checkpoint = Path(checkpoint).resolve()
        self.device_name = device
        self.mask_threshold = float(mask_threshold)
        self.min_water_fraction = float(min_water_fraction)
        if not 0 < self.mask_threshold < 1:
            raise ValueError('mask_threshold必须在0和1之间。')
        if not 0 < self.min_water_fraction < 1:
            raise ValueError('min_water_fraction必须在0和1之间。')
        self._lock = threading.Lock()
        self._runtime = None

    def load(self):
        if self._runtime is not None:
            return
        if not self.checkpoint.is_file():
            raise FileNotFoundError('未找到V-FloodNet图像分割权重。')
        try:
            import torch
            import torch.nn.functional as functional
            from PIL import Image
            from torchvision.transforms import functional as transforms
        except ImportError as error:
            raise RuntimeError('V-FloodNet环境缺少torch、torchvision或Pillow。') from error
        if self.device_name == 'auto':
            device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            device = torch.device(self.device_name)
        try:
            model = torch.load(self.checkpoint, map_location=device, weights_only=False)
        except TypeError:
            model = torch.load(self.checkpoint, map_location=device)
        if hasattr(model, 'module'):
            model = model.module
        if not callable(model) and not callable(getattr(model, 'predict', None)):
            raise RuntimeError('权重文件没有包含可运行的V-FloodNet图像分割模型。')
        model.to(device)
        model.eval()
        self._runtime = (torch, functional, Image, transforms, device, model)

    def analyze(self, image_bytes):
        self.load()
        torch, functional, Image, transforms, device, model = self._runtime
        try:
            with Image.open(io.BytesIO(image_bytes)) as opened:
                opened.load()
                image = opened.convert('RGB')
        except Exception as error:
            raise RequestError('图片无法解码。') from error
        original_height, original_width = image.height, image.width
        if original_width < 2 or original_height < 2 or original_width * original_height > 24_000_000:
            raise RequestError('图片尺寸无效或过大。')
        resized = transforms.resize(image, [416, 416])
        tensor = transforms.pil_to_tensor(resized).float().div(255.0)
        tensor = transforms.normalize(tensor, [0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        tensor = tensor.unsqueeze(0).to(device)
        with self._lock, torch.inference_mode():
            output = model.predict(tensor) if callable(getattr(model, 'predict', None)) else model(tensor)
            if isinstance(output, dict):
                output = output.get('out') if output.get('out') is not None else output.get('mask')
            elif isinstance(output, (tuple, list)):
                output = output[-1]
            if output is None or not hasattr(output, 'shape'):
                raise RuntimeError('模型未返回分割张量。')
            if output.ndim == 3:
                output = output.unsqueeze(1)
            if output.ndim != 4:
                raise RuntimeError('模型分割张量维度无效。')
            if output.shape[1] > 1:
                probability = torch.softmax(output, dim=1)[:, 1:2]
            else:
                probability = output
                minimum = float(probability.detach().min().cpu())
                maximum = float(probability.detach().max().cpu())
                if minimum < 0 or maximum > 1:
                    probability = torch.sigmoid(probability)
            probability = functional.interpolate(
                probability, size=(original_height, original_width), mode='bilinear', align_corners=False)
            water_fraction = float((probability >= self.mask_threshold).float().mean().cpu())
            certainty = float((probability - 0.5).abs().mul(2).mean().clamp(0, 1).cpu())
        detected = water_fraction >= self.min_water_fraction
        confidence = round(min(0.99, max(0.5, 0.5 + 0.49 * certainty)), 4)
        percentage = round(water_fraction * 100, 3)
        return {
            'water_detected': detected,
            'confidence': confidence,
            'quality': 'good',
            'summary': f'V-FloodNet分割水体像素占比约{percentage}%，结果仅作为待人工核验线索。',
        }


def make_handler(engine, api_key, model_name):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'VFloodNetLocal/1.0'

        def log_message(self, _format, *_args):
            return

        def _send(self, status, payload):
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != '/health':
                self._send(404, {'error': 'not_found'})
                return
            self._send(200, {'status': 'ok', 'model': model_name,
                             'checkpoint_loaded': engine._runtime is not None})

        def do_POST(self):
            if self.path != '/v1/chat/completions':
                self._send(404, {'error': 'not_found'})
                return
            supplied = self.headers.get('Authorization', '')
            expected = 'Bearer ' + api_key
            if not hmac.compare_digest(supplied, expected):
                self._send(401, {'error': 'unauthorized'})
                return
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                self._send(400, {'error': 'invalid_content_length'})
                return
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._send(413, {'error': 'request_too_large'})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                image_data, _ = extract_image_data(payload)
                result = engine.analyze(image_data)
            except RequestError:
                self._send(400, {'error': 'invalid_request'})
                return
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {'error': 'invalid_json'})
                return
            except Exception as error:
                print('V-FloodNet inference failed:', type(error).__name__, flush=True)
                self._send(503, {'error': 'inference_unavailable'})
                return
            self._send(200, chat_response(result, model_name))

    return Handler


def main():
    root = Path(__file__).resolve().parents[2]
    default_checkpoint = root / 'third_party' / 'V-FloodNet' / 'records' / 'link_efficientb4_model.pth'
    parser = argparse.ArgumentParser(description='V-FloodNet本机视觉sidecar，只监听回环地址')
    parser.add_argument('--checkpoint', type=Path, default=default_checkpoint)
    parser.add_argument('--host', default='127.0.0.1', choices=('127.0.0.1',))
    parser.add_argument('--port', type=int, default=8877)
    parser.add_argument('--device', default='auto', choices=('auto', 'cpu', 'cuda'))
    parser.add_argument('--api-key-env', default='V_FLOODNET_API_KEY')
    parser.add_argument('--model-name', default=DEFAULT_MODEL_NAME)
    parser.add_argument('--mask-threshold', type=float, default=0.5)
    parser.add_argument('--min-water-fraction', type=float, default=0.002)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在1到65535之间。')
    api_key = os.environ.get(args.api_key_env, '')
    if len(api_key) < 16:
        parser.error(args.api_key_env + '必须通过环境变量提供，且至少16个字符。')
    engine = VFloodNetEngine(args.checkpoint, device=args.device,
                             mask_threshold=args.mask_threshold,
                             min_water_fraction=args.min_water_fraction)
    try:
        engine.load()
    except Exception as error:
        parser.exit(1, 'V-FloodNet加载失败：' + str(error) + '\n')
    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine, api_key, args.model_name))
    print(f'V-FloodNet本机服务：http://{args.host}:{args.port}/v1/chat/completions', flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
