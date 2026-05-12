import os

SOURCE = "app/bot/commands.py"
DEST = "app/bot/routers/profile_ai.py"

def main():
    with open(SOURCE, "r", encoding="utf-8") as f:
        content = f.read()

    commands_to_move = [
        "bio", "mybirthday", "notes", "remember", "mood", "linkfilter", "setflavor",
        "setnika", "nika", "aiimage", "signai", "setsignprice", "delsignprice", 
        "signprice", "signorders", "signstats", "signreq"
    ]
    
    lines = content.split("\n")
    
    output_lines = []
    remaining_lines = []
    
    in_target_handler = False
    
    for line in lines:
        if line.strip().startswith("@router.") or line.strip().startswith("async def _download_single_reference_image") or line.strip().startswith("async def _download_reference_images") or line.strip().startswith("def _reference_image_spec") or line.strip().startswith("async def _charge_after_success"):
            is_target = False
            for cmd in commands_to_move:
                if f'Command("{cmd}"' in line or f'Command("{cmd}",' in line or line.strip().startswith("async def _download_") or line.strip().startswith("def _reference_image") or line.strip().startswith("async def _charge_"):
                    is_target = True
                    break
            
            in_target_handler = is_target

        if in_target_handler or line.strip() == 'NIKA_REFERENCE_ASSET_KEY = "nika_reference"':
            pass

        if in_target_handler:
            output_lines.append(line)
        else:
            remaining_lines.append(line)

    with open("scratch/profile_raw.py", "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines))
        
    print(f"Extracted {len(output_lines)} lines for profile.")

    with open(SOURCE, "w", encoding="utf-8") as f:
        f.write("\n".join(remaining_lines))

if __name__ == "__main__":
    main()
