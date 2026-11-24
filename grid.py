import math
import os
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

def build_grid(items: list[dict], out_path: str) -> None:
    padding = 16
    poster_w, poster_h = 342, 513
    label_h = 84 if any(item.get("subtitle") for item in items) else 60
    tile_w = poster_w
    tile_h = poster_h + label_h
    
    num_items = len(items)
    # Squarish layout: 2 -> 2 cols, 3-4 -> 2 cols, 5-9 -> 3 cols
    cols = max(1, math.ceil(math.sqrt(num_items)))
    rows = math.ceil(num_items / cols)
    grid_w = padding * (cols + 1) + tile_w * cols
    grid_h = padding * (rows + 1) + tile_h * rows

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
        row, col = divmod(i, cols)
        # Center a short last row
        in_row = min(cols, num_items - row * cols)
        row_offset = (cols - in_row) * (tile_w + padding) // 2
        x = padding + row_offset + col * (tile_w + padding)
        y = padding + row * (tile_h + padding)
        
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

        subtitle = item.get("subtitle")
        if subtitle:
            draw.text((x, ly + 48), subtitle, fill="#fff", font=font_med_bold)

    img.convert("RGB").save(out_path, "JPEG", quality=85)

if __name__ == "__main__":
    titles = [("Scream", "1996", "In Library"), ("Scream 2", "1997", "Requested"), ("Scream 3", "2000", None),
              ("Scream 4", "2011", None), ("Scream (2022)", "2022", "In Library"), ("Scream VI", "2023", None)]
    import sys
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    fake_items = [{"n": i + 1, "poster_url": None, "title": titles[i % 6][0], "year": titles[i % 6][1],
                   "badge": titles[i % 6][2]} for i in range(count)]
    build_grid(fake_items, f"sample{count}.jpg")
