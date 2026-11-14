"""Normalization, safe image fetching, and PNG rendering for level-up alerts."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageEnhance, ImageFont, ImageOps

from utils.http import safe_http_request


CANVAS_WIDTH = 1200
CANVAS_HEIGHT = 630
MIN_CANVAS_WIDTH = 320
MIN_CANVAS_HEIGHT = 180
MAX_CANVAS_SIZE = 2000
# Bound decoded size of linked images: a tiny compressed PNG can otherwise expand to hundreds of MiB.
MAX_LINKED_IMAGE_PIXELS = 16_000_000
MAX_LINKED_IMAGE_SIDE = 4096
MAX_IMAGE_LAYERS = 6
MAX_TEXT_LAYERS = 16
MAX_BAR_LAYERS = 6
FONT_FAMILIES = {'outfit', 'inter', 'montserrat', 'serif', 'mono'}
FONT_WEIGHTS = {400, 600, 700, 800}
ALIGNMENTS = {'left', 'center', 'right'}
IMAGE_FITS = {'cover', 'contain'}
IMAGE_SOURCES = {'url', 'avatar'}
DYNAMIC_IMAGE_URLS = {'{avatar}', '{server_icon}', '{server_banner}'}
HEX_COLOR = re.compile(r'^#[0-9a-f]{6}$', re.IGNORECASE)
SAFE_LAYER_ID = re.compile(r'[^a-z0-9_-]', re.IGNORECASE)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE_PATHS = (
    PROJECT_ROOT / 'static' / 'level-alert-default.png',
    PROJECT_ROOT / 'frontend' / 'public' / 'level-alert-default.png',
)


def _text_layer(
    layer_id: str,
    name: str,
    text: str,
    x: int,
    y: int,
    size: int,
    weight: int = 700,
    color: str = '#ffffff',
    align: str = 'left',
) -> dict[str, Any]:
    return {
        'id': layer_id,
        'name': name,
        'text': text,
        'x': x,
        'y': y,
        'font_family': 'outfit',
        'font_size': size,
        'font_weight': weight,
        'color': color,
        'align': align,
    }


def _image_layer(layer_id: str = 'image-1', name: str = 'Image 1') -> dict[str, Any]:
    return {
        'id': layer_id,
        'name': name,
        'source': 'url',
        'url': '',
        'x': 50,
        'y': 50,
        'width': 100,
        'height': 100,
        'opacity': 100,
        'fit': 'cover',
    }


def _bar_layer(layer_id: str = 'bar-1', name: str = 'XP Bar 1') -> dict[str, Any]:
    return {
        'id': layer_id,
        'name': name,
        'x': 50,
        'y': 72,
        'width': 60,
        'height': 5,
        'track_color': '#272033',
        'fill_color': '#8b5cf6',
        'radius': 50,
    }


DEFAULT_LEVEL_ALERT_IMAGE: dict[str, Any] = {
    'mode': 'embed',
    'image_card': {
        'width': CANVAS_WIDTH,
        'height': CANVAS_HEIGHT,
        'background_color': '#08070f',
        'overlay': 28,
        'images': [
            {**_image_layer(), 'name': 'Background'},
            {
                'id': 'avatar',
                'name': 'Profile picture',
                'source': 'avatar',
                'url': '',
                'x': 22,
                'y': 50,
                'width': 24,
                'height': 45.714,
                'opacity': 100,
                'fit': 'cover',
            },
        ],
        'order': [
            'text:title',
            'text:member',
            'text:level',
            'text:xp',
            'bar:progress',
            'image:avatar',
            'image:image-1',
        ],
        'texts': [
            _text_layer('title', 'Title', 'LEVEL UP!', 39, 20, 38, 700, '#c4b5fd'),
            _text_layer('member', 'Member', '{mention}', 39, 37, 68, 800),
            _text_layer('level', 'Level', 'LEVEL {level}', 39, 56, 46, 700),
            _text_layer('xp', 'XP progress', '{current_xp} / {xp_needed} XP', 39, 71, 26, 600, '#ddd6fe'),
        ],
        'bars': [
            {
                **_bar_layer('progress', 'XP progress'),
                'x': 67,
                'y': 82,
                'width': 56,
                'height': 3,
                'track_color': '#2a2140',
                'fill_color': '#a78bfa',
                'radius': 100,
            }
        ],
    },
}


def _number(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _color(value: Any, default: str) -> str:
    text = str(value or '').strip().lower()
    return text if HEX_COLOR.fullmatch(text) else default


def _image_url(value: Any) -> str:
    text = str(value or '').strip()[:2048]
    if text in DYNAMIC_IMAGE_URLS:
        return text
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ''
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        return ''
    hostname = parsed.hostname.casefold().strip('.')
    if hostname == 'localhost' or hostname.endswith('.local'):
        return ''
    return text


def _normalize_images(value: Any, canvas_width: int, canvas_height: int) -> list[dict[str, Any]]:
    is_explicit_list = isinstance(value, list)
    source = value if is_explicit_list else []
    output: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(source[:MAX_IMAGE_LAYERS]):
        if not isinstance(raw, dict):
            continue
        default = _image_layer(f'image-{index + 1}')
        layer_id = SAFE_LAYER_ID.sub('-', str(raw.get('id') or default['id']).lower())[:40].strip('-')
        if not layer_id or layer_id in used_ids:
            layer_id = f'image-{index + 1}'
        used_ids.add(layer_id)
        fit = str(raw.get('fit', default['fit'])).lower()
        source_type = str(raw.get('source', default['source'])).lower()
        normalized_source = source_type if source_type in IMAGE_SOURCES else default['source']
        width = _number(raw.get('width'), default['width'], 1, 200)
        height = round(width * canvas_width / canvas_height, 3) if normalized_source == 'avatar' else _number(raw.get('height'), default['height'], 1, 200)
        output.append(
            {
                'id': layer_id,
                'name': str(raw.get('name') or f'Image {index + 1}').strip()[:40] or f'Image {index + 1}',
                'source': normalized_source,
                'url': _image_url(raw.get('url')),
                'x': _number(raw.get('x'), default['x'], 0, 100),
                'y': _number(raw.get('y'), default['y'], 0, 100),
                'width': width,
                'height': height,
                'opacity': _number(raw.get('opacity'), default['opacity'], 0, 100),
                'fit': fit if fit in IMAGE_FITS else default['fit'],
            }
        )
    return output if is_explicit_list else output or [_image_layer()]


def _normalize_texts(value: Any, legacy_value: Any) -> list[dict[str, Any]]:
    defaults = DEFAULT_LEVEL_ALERT_IMAGE['image_card']['texts']
    defaults_by_id = {layer['id']: layer for layer in defaults}
    legacy_names = {'title': 'Title', 'member': 'Member', 'level': 'Level', 'xp': 'Lifetime XP'}

    if isinstance(value, list):
        source = value
    elif isinstance(legacy_value, dict):
        source = [{**default, **(legacy_value.get(default['id']) or {})} for default in defaults]
        source.extend({'id': layer_id, 'name': legacy_names.get(layer_id, layer_id.title()), **raw} for layer_id, raw in legacy_value.items() if layer_id not in defaults_by_id and isinstance(raw, dict))
    else:
        return deepcopy(defaults)

    output: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(source[:MAX_TEXT_LAYERS]):
        if not isinstance(raw, dict):
            continue
        proposed_id = SAFE_LAYER_ID.sub('-', str(raw.get('id') or f'text-{index + 1}').lower())[:40].strip('-')
        layer_id = proposed_id if proposed_id and proposed_id not in used_ids else f'text-{index + 1}'
        while layer_id in used_ids:
            layer_id = f'{layer_id}-{index + 1}'
        used_ids.add(layer_id)
        default = deepcopy(defaults_by_id.get(layer_id) or _text_layer(layer_id, f'Text {index + 1}', '', 50, 50, 40, align='center'))
        family = str(raw.get('font_family', default['font_family'])).lower()
        weight = _number(raw.get('font_weight'), default['font_weight'], 400, 800)
        alignment = str(raw.get('align', default['align'])).lower()
        output.append(
            {
                'id': layer_id,
                'name': str(raw.get('name') or default['name']).strip()[:40] or default['name'],
                'text': str(raw.get('text', default['text']))[:300],
                'x': _number(raw.get('x'), default['x'], 4, 96),
                'y': _number(raw.get('y'), default['y'], 5, 95),
                'font_family': family if family in FONT_FAMILIES else default['font_family'],
                'font_size': _number(raw.get('font_size'), default['font_size'], 8, 300),
                'font_weight': min(FONT_WEIGHTS, key=lambda allowed: abs(allowed - weight)),
                'color': _color(raw.get('color'), default['color']),
                'align': alignment if alignment in ALIGNMENTS else default['align'],
            }
        )
    return output


def _normalize_bars(value: Any) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) else []
    output: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(source[:MAX_BAR_LAYERS]):
        if not isinstance(raw, dict):
            continue
        default = _bar_layer(f'bar-{index + 1}', f'XP Bar {index + 1}')
        layer_id = SAFE_LAYER_ID.sub('-', str(raw.get('id') or default['id']).lower())[:40].strip('-')
        if not layer_id or layer_id in used_ids:
            layer_id = f'bar-{index + 1}'
        used_ids.add(layer_id)
        output.append(
            {
                'id': layer_id,
                'name': str(raw.get('name') or default['name']).strip()[:40] or default['name'],
                'x': _number(raw.get('x'), default['x'], 0, 100),
                'y': _number(raw.get('y'), default['y'], 0, 100),
                'width': _number(raw.get('width'), default['width'], 1, 100),
                'height': _number(raw.get('height'), default['height'], 1, 100),
                'track_color': _color(raw.get('track_color'), default['track_color']),
                'fill_color': _color(raw.get('fill_color'), default['fill_color']),
                'radius': _number(raw.get('radius'), default['radius'], 0, 100),
            }
        )
    return output


def normalize_level_alert_visual(value: Any) -> dict[str, Any]:
    """Return a bounded, renderer-safe level-alert visual configuration."""
    result = deepcopy(DEFAULT_LEVEL_ALERT_IMAGE)
    if not isinstance(value, dict):
        return result

    result['mode'] = 'image' if value.get('mode') == 'image' else 'embed'
    source = value.get('image_card')
    if not isinstance(source, dict):
        source = {}
    if not source:
        return result
    target = result['image_card']
    target['width'] = _number(source.get('width'), target['width'], MIN_CANVAS_WIDTH, MAX_CANVAS_SIZE)
    target['height'] = _number(source.get('height'), target['height'], MIN_CANVAS_HEIGHT, MAX_CANVAS_SIZE)
    target['background_color'] = _color(source.get('background_color'), target['background_color'])
    target['overlay'] = _number(source.get('overlay'), target['overlay'], 0, 80)
    target['images'] = _normalize_images(source.get('images'), target['width'], target['height'])
    target['texts'] = _normalize_texts(source.get('texts'), source.get('layers'))
    target['bars'] = _normalize_bars(source.get('bars'))
    text_keys = [f'text:{layer["id"]}' for layer in target['texts']]
    bar_keys = [f'bar:{layer["id"]}' for layer in target['bars']]
    available_order = text_keys + bar_keys + [f'image:{layer["id"]}' for layer in target['images']]
    raw_order = source.get('order') if isinstance(source.get('order'), list) else []
    migrated_order = [f'text:{key}' if f'text:{key}' in text_keys else str(key) for key in raw_order]
    target['order'] = list(dict.fromkeys([key for key in migrated_order if key in available_order] + available_order))
    return result


async def fetch_level_alert_images(
    settings: Any,
    avatar_url: str = '',
    server_icon_url: str = '',
    server_banner_url: str = '',
) -> dict[str, bytes]:
    """Fetch linked image layers while blocking private-network and oversized responses."""
    card = normalize_level_alert_visual(settings)['image_card']
    payloads: dict[str, bytes] = {}
    for layer in card['images']:
        if layer['source'] == 'avatar':
            source_url = avatar_url
        elif layer['url'] == '{avatar}':
            source_url = avatar_url
        elif layer['url'] == '{server_icon}':
            source_url = server_icon_url
        elif layer['url'] == '{server_banner}':
            source_url = server_banner_url
        else:
            source_url = layer['url']
        if not source_url:
            continue
        try:
            status, _, body, _ = await safe_http_request(
                'GET',
                source_url,
                allowed_domains=[],
                headers={'Accept': 'image/png,image/jpeg,image/webp'},
                timeout_seconds=8,
            )
            if status != 200:
                continue
            with Image.open(BytesIO(body)) as linked:
                if not _within_decode_limits(linked):
                    continue
                linked.verify()
            payloads[layer['id']] = body
        except Exception:
            continue
    return payloads


def _font(layer: dict[str, Any]) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    family = layer['font_family']
    bold = layer['font_weight'] >= 700
    candidates = {
        'serif': [
            'DejaVuSerif-Bold.ttf' if bold else 'DejaVuSerif.ttf',
            '/usr/share/fonts/google-noto/NotoSerif-Bold.ttf' if bold else '/usr/share/fonts/google-noto/NotoSerif-Regular.ttf',
            'C:/Windows/Fonts/timesbd.ttf' if bold else 'C:/Windows/Fonts/times.ttf',
        ],
        'mono': [
            'DejaVuSansMono-Bold.ttf' if bold else 'DejaVuSansMono.ttf',
            '/usr/share/fonts/google-noto/NotoSansMono-Bold.ttf' if bold else '/usr/share/fonts/google-noto/NotoSansMono-Regular.ttf',
            'C:/Windows/Fonts/courbd.ttf' if bold else 'C:/Windows/Fonts/cour.ttf',
        ],
    }.get(
        family,
        [
            'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf',
            '/usr/share/fonts/google-noto/NotoSans-Bold.ttf' if bold else '/usr/share/fonts/google-noto/NotoSans-Regular.ttf',
            '/usr/share/fonts/open-sans/OpenSans-Bold.ttf' if bold else '/usr/share/fonts/open-sans/OpenSans-Regular.ttf',
            'C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf',
        ],
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, layer['font_size'])
        except OSError:
            continue
    return ImageFont.load_default(size=layer['font_size'])


def _within_decode_limits(image: Image.Image) -> bool:
    width, height = image.size
    return 0 < width <= MAX_LINKED_IMAGE_SIDE and 0 < height <= MAX_LINKED_IMAGE_SIDE and (width * height <= MAX_LINKED_IMAGE_PIXELS)


def _open_image(payload: bytes) -> Image.Image | None:
    try:
        with Image.open(BytesIO(payload)) as image:
            # Header-only check before any pixel data is decoded.
            if not _within_decode_limits(image):
                return None
            # JPEG can decode at reduced scale; others are downscaled before the RGBA conversion.
            image.draft('RGB', (MAX_CANVAS_SIZE, MAX_CANVAS_SIZE))
            image.thumbnail((MAX_CANVAS_SIZE, MAX_CANVAS_SIZE))
            return image.convert('RGBA')
    except Exception:
        return None


def _load_default_image() -> Image.Image | None:
    for path in DEFAULT_IMAGE_PATHS:
        if not path.is_file():
            continue
        try:
            with Image.open(path) as image:
                return image.convert('RGBA')
        except Exception:
            continue
    return None


def _fit_image(source: Image.Image, size: tuple[int, int], fit: str) -> Image.Image:
    if fit == 'contain':
        contained = ImageOps.contain(source, size, Image.Resampling.LANCZOS)
        output = Image.new('RGBA', size, (0, 0, 0, 0))
        output.alpha_composite(contained, ((size[0] - contained.width) // 2, (size[1] - contained.height) // 2))
        return output
    return ImageOps.fit(source, size, Image.Resampling.LANCZOS)


def render_level_alert_image(
    settings: Any,
    values: dict[str, str],
    image_payloads: dict[str, bytes] | None = None,
    use_default_image: bool = True,
) -> bytes:
    """Render a configured level-up card at its selected resolution."""
    visual = normalize_level_alert_visual(settings)
    card = visual['image_card']
    canvas_size = (card['width'], card['height'])
    canvas = Image.new('RGBA', canvas_size, ImageColor.getrgb(card['background_color']) + (255,))
    payloads = image_payloads or {}

    if card['overlay']:
        shade = Image.new('RGBA', canvas_size, (0, 0, 0, round(255 * card['overlay'] / 100)))
        canvas = Image.alpha_composite(canvas, shade)

    images_by_key = {f'image:{layer["id"]}': (index, layer) for index, layer in enumerate(card['images'])}
    texts_by_key = {f'text:{layer["id"]}': layer for layer in card['texts']}
    bars_by_key = {f'bar:{layer["id"]}': layer for layer in card['bars']}
    for key in reversed(card['order']):
        image_entry = images_by_key.get(key)
        if image_entry:
            index, layer = image_entry
            source = _open_image(payloads[layer['id']]) if layer['id'] in payloads else None
            if use_default_image and source is None and layer['source'] == 'url' and index == 0 and not layer['url']:
                source = _load_default_image()
            if source is None:
                continue
            layer_size = (
                max(1, round(card['width'] * layer['width'] / 100)),
                max(1, round(card['height'] * layer['height'] / 100)),
            )
            fitted = _fit_image(source, layer_size, layer['fit'])
            if layer['source'] == 'avatar':
                mask = Image.new('L', fitted.size, 0)
                ImageDraw.Draw(mask).ellipse((0, 0, fitted.width - 1, fitted.height - 1), fill=255)
                fitted.putalpha(ImageChops.multiply(fitted.getchannel('A'), mask))
            if card['overlay'] and layer['source'] == 'url' and index == 0:
                fitted = ImageEnhance.Brightness(fitted).enhance(max(0.2, 1 - card['overlay'] / 100))
            if layer['opacity'] < 100:
                alpha = fitted.getchannel('A').point(lambda value: round(value * layer['opacity'] / 100))
                fitted.putalpha(alpha)
            position = (
                round(card['width'] * layer['x'] / 100 - fitted.width / 2),
                round(card['height'] * layer['y'] / 100 - fitted.height / 2),
            )
            layer_canvas = Image.new('RGBA', canvas_size, (0, 0, 0, 0))
            layer_canvas.paste(fitted, position, fitted)
            canvas = Image.alpha_composite(canvas, layer_canvas)
            continue

        bar = bars_by_key.get(key)
        if bar:
            try:
                current_xp = float(str(values.get('current_xp', '0')).replace(',', ''))
                xp_needed = float(str(values.get('xp_needed', '0')).replace(',', ''))
            except (TypeError, ValueError):
                current_xp = 0
                xp_needed = 0
            progress = max(0.0, min(1.0, current_xp / xp_needed)) if xp_needed > 0 else 0.0
            width = max(1, round(card['width'] * bar['width'] / 100))
            height = max(1, round(card['height'] * bar['height'] / 100))
            left = round(card['width'] * bar['x'] / 100 - width / 2)
            top = round(card['height'] * bar['y'] / 100 - height / 2)
            radius = min(height // 2, round(height * bar['radius'] / 100))
            draw = ImageDraw.Draw(canvas)
            bounds = (left, top, left + width, top + height)
            draw.rounded_rectangle(bounds, radius=radius, fill=ImageColor.getrgb(bar['track_color']))
            filled_width = round(width * progress)
            if filled_width > 0:
                fill_bounds = (left, top, left + filled_width, top + height)
                draw.rounded_rectangle(fill_bounds, radius=min(radius, filled_width // 2), fill=ImageColor.getrgb(bar['fill_color']))
            continue

        layer = texts_by_key.get(key)
        if not layer:
            continue
        text = layer['text']
        for key, replacement in values.items():
            text = text.replace(f'{{{key}}}', replacement)
        if not text:
            continue
        x = round(card['width'] * layer['x'] / 100)
        y = round(card['height'] * layer['y'] / 100)
        anchor = {'left': 'lm', 'center': 'mm', 'right': 'rm'}[layer['align']]
        ImageDraw.Draw(canvas).multiline_text(
            (x, y),
            text,
            font=_font(layer),
            fill=ImageColor.getrgb(layer['color']),
            anchor=anchor,
            align=layer['align'],
            spacing=8,
            stroke_width=2,
            stroke_fill=(0, 0, 0, 150),
        )

    output = BytesIO()
    canvas.convert('RGB').save(output, format='PNG', optimize=True)
    return output.getvalue()
