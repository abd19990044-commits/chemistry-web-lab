from __future__ import annotations

import io
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw

from . import publication_renderer as base
from . import publication_renderer_v2 as v2

RENDER_DPI = 600
DEFAULT_FONT_PX = 40
DEFAULT_GAP_PX = 20
DEFAULT_ARROW_PX = 200
INK = v2.INK
WHITE = v2.WHITE
ARROW_LINE_WIDTH = 2
ARROW_HEAD = 14
ARROW_HEAD_HALF_HEIGHT = 6

split_reaction_side = v2.split_reaction_side
formula_for_display = v2.formula_for_display


def _build_tokens(reactant_pairs, product_pairs, small_as_formula: bool):
    return v2._build_tokens(reactant_pairs, product_pairs, small_as_formula)


def _transparentize(img: Image.Image) -> Image.Image:
    # Preserve the source bot's near-white threshold without allocating two
    # Python tuple lists per image (hundreds of MB for a large drawing).
    from PIL import ImageChops
    rgba = img.convert("RGBA")
    red, green, blue, alpha = rgba.split()
    minimum = ImageChops.darker(ImageChops.darker(red, green), blue)
    ink_mask = minimum.point(lambda value: 0 if value > 248 else 255)
    rgba.putalpha(ImageChops.darker(alpha, ink_mask))
    return rgba


def _draw_arrow_png(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, style: str) -> None:
    end = x + w
    head = ARROW_HEAD
    half = ARROW_HEAD_HALF_HEIGHT
    lw = ARROW_LINE_WIDTH
    if style == "equilibrium":
        off = 5
        draw.line((x, y - off, end - head, y - off), fill=INK, width=lw)
        draw.polygon([(end, y-off), (end-head, y-off-half), (end-head, y-off+half)], fill=INK)
        draw.line((x + head, y + off, end, y + off), fill=INK, width=lw)
        draw.polygon([(x, y+off), (x+head, y+off-half), (x+head, y+off+half)], fill=INK)
    elif style == "reversible":
        draw.line((x + head, y, end - head, y), fill=INK, width=lw)
        draw.polygon([(end, y), (end-head, y-half), (end-head, y+half)], fill=INK)
        draw.polygon([(x, y), (x+head, y-half), (x+head, y+half)], fill=INK)
    else:
        draw.line((x, y, end - head, y), fill=INK, width=lw)
        draw.polygon([(end, y), (end-head, y-half), (end-head, y+half)], fill=INK)


def _svg_arrow(x: int, cy: int, w: int, style: str) -> str:
    end = x + w - 5
    head = ARROW_HEAD
    half = ARROW_HEAD_HALF_HEIGHT
    lw = ARROW_LINE_WIDTH
    if style == "equilibrium":
        off = 5
        return (
            f'<line x1="{x}" y1="{cy-off}" x2="{end-head}" y2="{cy-off}" stroke="#141414" stroke-width="{lw}"/>'
            f'<path d="M {end} {cy-off} L {end-head} {cy-off-half} L {end-head} {cy-off+half} Z" fill="#141414"/>'
            f'<line x1="{x+head}" y1="{cy+off}" x2="{end}" y2="{cy+off}" stroke="#141414" stroke-width="{lw}"/>'
            f'<path d="M {x} {cy+off} L {x+head} {cy+off-half} L {x+head} {cy+off+half} Z" fill="#141414"/>'
        )
    if style == "reversible":
        return (
            f'<line x1="{x+head}" y1="{cy}" x2="{end-head}" y2="{cy}" stroke="#141414" stroke-width="{lw}"/>'
            f'<path d="M {end} {cy} L {end-head} {cy-half} L {end-head} {cy+half} Z" fill="#141414"/>'
            f'<path d="M {x} {cy} L {x+head} {cy-half} L {x+head} {cy+half} Z" fill="#141414"/>'
        )
    return (
        f'<line x1="{x}" y1="{cy}" x2="{end-head}" y2="{cy}" stroke="#141414" stroke-width="{lw}"/>'
        f'<path d="M {end} {cy} L {end-head} {cy-half} L {end-head} {cy+half} Z" fill="#141414"/>'
    )


def render_reaction_png(
    reactant_pairs,
    product_pairs,
    *,
    small_as_formula: bool = True,
    arrow_top: str = "",
    arrow_bottom: str = "",
    dpi: int = RENDER_DPI,
    arrow_style: str = "forward",
    transparent_background: bool = False,
) -> bytes | None:
    tokens = _build_tokens(reactant_pairs, product_pairs, small_as_formula)
    if not tokens:
        return None

    rendered = []
    font = base._load_font(DEFAULT_FONT_PX)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    ann_w = max(base._annotation_width(arrow_top), base._annotation_width(arrow_bottom))
    arrow_w = max(DEFAULT_ARROW_PX, ann_w + 64)

    for kind, payload in tokens:
        if kind == "mol":
            image = v2._molecule_png(str(payload))
            if image is None:
                return None
            rendered.append((kind, image, image.width, image.height))
        elif kind == "condensed":
            image = v2._condensed_png(str(payload), DEFAULT_FONT_PX)
            rendered.append((kind, image, image.width, image.height))
        elif kind == "formula":
            image = base._formula_png(str(payload), DEFAULT_FONT_PX)
            rendered.append((kind, image, image.width, image.height))
        elif kind in {"op", "coef"}:
            box = probe.textbbox((0, 0), str(payload), font=font)
            rendered.append((kind, payload, max(1, box[2]-box[0]), max(1, box[3]-box[1])))
        else:
            rendered.append((kind, None, arrow_w, 28))

    margin_x = 34
    top_room = 42 if arrow_top else 0
    bottom_room = 38 if arrow_bottom else 0
    chemistry_h = max((h for _, _, _, h in rendered), default=104)
    width = sum(w for _, _, w, _ in rendered) + DEFAULT_GAP_PX * (len(rendered)-1) + 2*margin_x
    height = chemistry_h + top_room + bottom_room + 48

    if width > 16000 or height > 4000 or width * height > 16000000:
        raise ValueError("This reaction exceeds the drawing canvas limit. Use fewer species.")
    mode = "RGBA" if transparent_background else "RGB"
    bg = (255,255,255,0) if transparent_background else WHITE
    canvas = Image.new(mode, (max(1,width), max(1,height)), bg)
    draw = ImageDraw.Draw(canvas)
    center_y = top_room + 24 + chemistry_h // 2
    x = margin_x

    for kind, payload, w, h in rendered:
        if kind in {"mol", "formula", "condensed"}:
            image = payload
            if transparent_background:
                image = _transparentize(image)
                canvas.alpha_composite(image, (x, center_y-image.height//2))
            else:
                canvas.paste(image, (x, center_y-image.height//2))
        elif kind == "arrow":
            _draw_arrow_png(draw, x, center_y, w, arrow_style)
            cx = x + w//2
            if arrow_top:
                base._draw_annotation(draw, cx, center_y-15, arrow_top, font_size=28)
            if arrow_bottom:
                base._draw_annotation(draw, cx, center_y+34, arrow_bottom, font_size=28)
        else:
            box = draw.textbbox((0,0), str(payload), font=font)
            draw.text((x-box[0], center_y-(box[3]-box[1])//2-box[1]), str(payload), fill=INK, font=font)
        x += w + DEFAULT_GAP_PX

    out = io.BytesIO()
    canvas.save(out, format="PNG", dpi=(dpi,dpi), compress_level=6)
    return out.getvalue()


def render_reaction_svg(
    reactant_pairs,
    product_pairs,
    *,
    small_as_formula: bool = True,
    arrow_top: str = "",
    arrow_bottom: str = "",
    arrow_style: str = "forward",
    transparent_background: bool = False,
) -> bytes | None:
    tokens = _build_tokens(reactant_pairs, product_pairs, small_as_formula)
    if not tokens:
        return None

    items = []
    ann_w = max(len(arrow_top), len(arrow_bottom)) * 14
    arrow_w = max(DEFAULT_ARROW_PX, ann_w + 64)
    for kind, payload in tokens:
        if kind == "mol":
            frag = v2._molecule_svg_fragment(str(payload))
            if frag is None:
                return None
            items.append((kind, frag, frag[1], frag[2]))
        elif kind == "condensed":
            parts = v2._split_condensed(str(payload))
            approx_w = 0
            for pk, pv in parts:
                approx_w += max(34, int(len(pv)*DEFAULT_FONT_PX*0.47+8)) if pk == "group" else max(32,int(DEFAULT_FONT_PX*0.90))+2*max(4,int(DEFAULT_FONT_PX*0.10))
            items.append((kind, payload, approx_w, 60))
        elif kind == "formula":
            items.append((kind, payload, max(64,len(str(payload))*22+16), 56))
        elif kind in {"op","coef"}:
            items.append((kind,payload,max(32,len(str(payload))*22),50))
        else:
            items.append((kind,None,arrow_w,28))

    margin = 34
    top = 48 if arrow_top else 16
    bottom = 44 if arrow_bottom else 20
    chemistry_h = max((h for _,_,_,h in items), default=104)
    width = sum(w for _,_,w,_ in items) + DEFAULT_GAP_PX*(len(items)-1) + 2*margin
    height = top + chemistry_h + bottom
    cy = top + chemistry_h//2

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    if not transparent_background:
        out.append('<rect width="100%" height="100%" fill="white"/>')
    out.append('<style>text{font-family:Arial,Helvetica,sans-serif;fill:#141414}</style>')

    x = margin
    for kind,payload,w,h in items:
        if kind == "mol":
            frag,_,_ = payload
            out.append(f'<g transform="translate({x},{cy-h//2})">{frag}</g>')
        elif kind == "condensed":
            frag,_,_ = v2._condensed_svg(str(payload), x, cy, DEFAULT_FONT_PX)
            out.append(frag)
        elif kind == "formula":
            out.append(v2._svg_formula(str(payload), x+5, cy+13, DEFAULT_FONT_PX))
        elif kind in {"op","coef"}:
            out.append(f'<text x="{x}" y="{cy+13}" font-size="{DEFAULT_FONT_PX}" font-weight="400">{escape(str(payload))}</text>')
        else:
            out.append(_svg_arrow(x, cy, w, arrow_style))
            cx = x+w//2
            if arrow_top:
                out.append(base._svg_annotation(arrow_top,cx,cy-15,font_size=28))
            if arrow_bottom:
                out.append(base._svg_annotation(arrow_bottom,cx,cy+33,font_size=28))
        x += w + DEFAULT_GAP_PX

    out.append('</svg>')
    return ''.join(out).encode('utf-8')


def render_reaction_scheme(reactant_pairs, product_pairs, sub_size=None, small_as_formula=True, arrow_top="", arrow_bottom="", arrow_style="forward", transparent_background=False):
    return render_reaction_png(
        reactant_pairs, product_pairs,
        small_as_formula=small_as_formula,
        arrow_top=arrow_top, arrow_bottom=arrow_bottom,
        arrow_style=arrow_style,
        transparent_background=transparent_background,
    )
