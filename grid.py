import os
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

def build_grid(items: list[dict], out_path: str) -> None:
    padding = 16
    poster_w, poster_h = 342, 513
    label_h = 60
    tile_w = poster_w
    tile_h = poster_h + label_h
    
    num_items = len(items)
    grid_w = (padding * (num_items + 1)) + (tile_w * num_items)
    grid_h = padding * 2 + tile_h

    img = Image.new("RGBA", (grid_w, grid_h), "#111")
    draw = ImageDraw.Draw(img)
    
    font_path_regular = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    font_path_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    
    try:
        font_large_bold = ImageFont.truetype(font_path_bold, 36)
        font_med_bold = ImageFont.truetype(font_path_bold, 24)
        font_small = ImageFont.truetype(font_path_regular, 18)
        font_tiny_bold = ImageFont.truetype(font_path_bold, 14)
    except IOError:
        font_large_bold = ImageFont.load_default(size=36)
        font_med_bold = ImageFont.load_default(size=24)
        font_small = ImageFont.load_default(size=18)
        font_tiny_bold = ImageFont.load_default(size=14)

    for i, item in enumerate(items):
        x = padding + i * (tile_w + padding)
        y = padding
        
        poster_img = None
        if item.get("poster_url"):
            try:
                resp = requests.get(item["poster_url"], timeout=10)
                resp.raise_for_status()
                poster_img = Image.open(BytesIO(resp.content)).convert("RGBA")
            except Exception:
                pass
        
        if poster_img:
            target_aspect = poster_w / poster_h
            img_aspect = poster_img.width / poster_img.height
            
            if img_aspect > target_aspect:
                new_w = int(poster_img.height * target_aspect)
                left = (poster_img.width - new_w) // 2
                poster_img = poster_img.crop((left, 0, left + new_w, poster_img.height))
            else:
                new_h = int(poster_img.width / target_aspect)
                top = (poster_img.height - new_h) // 2
                poster_img = poster_img.crop((0, top, poster_img.width, top + new_h))
                
            poster_img = poster_img.resize((poster_w, poster_h), Image.Resampling.LANCZOS)
            img.paste(poster_img, (x, y))
        else:
            draw.rectangle([x, y, x + poster_w, y + poster_h], fill="#333")
            title = item.get("title", "")
            words = title.split()
            lines = []
            curr_line = ""
            for w in words:
                test_line = curr_line + (" " if curr_line else "") + w
                if font_med_bold.getlength(test_line) > poster_w - 32:
                    if curr_line:
                        lines.append(curr_line)
                    curr_line = w
                else:
                    curr_line = test_line
            if curr_line:
                lines.append(curr_line)
            
            text_y = y + poster_h // 2 - (len(lines) * 30) // 2
            for line in lines:
                lw = font_med_bold.getlength(line)
                text_x = x + (poster_w - lw) // 2
                draw.text((text_x, text_y), line, fill="#ccc", font=font_med_bold)
                text_y += 30

        badge = item.get("badge")
        if badge:
            badge_lower = badge.lower()
            badge_color = None
            if "library" in badge_lower:
                badge_color = "#2e7d32"
                badge_text = "IN LIBRARY"
            elif "requested" in badge_lower:
                badge_color = "#f57c00"
                badge_text = "REQUESTED"
                
            if badge_color:
                bw = font_tiny_bold.getlength(badge_text) + 16
                bh = 24 
                bx = x + 8
                by = y + 8
                draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh//2, fill=badge_color)
                draw.text((bx + 8, by + 4), badge_text, fill="#fff", font=font_tiny_bold)

        cx = x + poster_w - 36
        cy = y + poster_h - 36
        draw.ellipse([cx - 28, cy - 28, cx + 28, cy + 28], fill=(0, 0, 0, 180))
        n_text = f"#{item['n']}"
        ntw = font_med_bold.getlength(n_text)
        draw.text((cx - ntw//2, cy - 14), n_text, fill="#fff", font=font_med_bold)

        ly = y + poster_h + 16
        draw.text((x, ly), n_text, fill="#fff", font=font_large_bold)
        
        n_width = font_large_bold.getlength(n_text)
        sub_x = x + n_width + 12
        sub_text = f"{item['title']} ({item.get('year', '')})"
        
        while True:
            sub_width = font_small.getlength(sub_text)
            if sub_x + sub_width > x + poster_w and len(sub_text) > 3:
                sub_text = sub_text[:-4] + "..."
            else:
                break
                
        sub_y = ly + 14
        draw.text((sub_x, sub_y), sub_text, fill="#aaa", font=font_small)

    img.convert("RGB").save(out_path, "JPEG", quality=85)

if __name__ == "__main__":
    fake_items = [
        {"n": 1, "poster_url": None, "title": "Scream", "year": "1996", "badge": "In Library"},
        {"n": 2, "poster_url": None, "title": "Scream 2", "year": "1997", "badge": "Requested"},
        {"n": 3, "poster_url": None, "title": "Scream 3", "year": "2000", "badge": None},
    ]
    build_grid(fake_items, "sample.jpg")
