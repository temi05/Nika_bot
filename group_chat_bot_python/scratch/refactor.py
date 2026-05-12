import os

SOURCE = "app/bot/commands.py"
RAW = "scratch/economy_raw.py"
DEST = "app/bot/routers/economy.py"

def main():
    with open(SOURCE, "r", encoding="utf-8") as f:
        content = f.read()
        
    with open(RAW, "r", encoding="utf-8") as f:
        raw_lines = f.read()

    # Create the final economy.py
    with open(DEST, "r", encoding="utf-8") as f:
        dest_content = f.read()
        
    final_dest = dest_content.replace("    # We will insert the raw lines here", raw_lines)
    with open(DEST, "w", encoding="utf-8") as f:
        f.write(final_dest)

    # Now, let's remove the raw lines from commands.py
    # We will use the same logic from the previous script to isolate the lines to keep
    commands_to_move = [
        "me", "top", "daily", "shop", "buy", "loan", "ask_loan", "repay", 
        "debts", "forgive", "jail", "judge", "bail", "give", "steal"
    ]
    
    lines = content.split("\n")
    remaining_lines = []
    in_target_handler = False
    
    for line in lines:
        if line.strip().startswith("@router.") or line.strip().startswith("def _get_rank_name"):
            is_target = False
            for cmd in commands_to_move:
                if f'Command("{cmd}"' in line or f'Command("{cmd}",' in line or f"startswith(\"top_\"" in line or f"def _show_top" in line:
                    is_target = True
                    break
            if line.strip().startswith("async def _show_top") or line.strip().startswith("def _get_rank_name"):
                is_target = True
            in_target_handler = is_target

        if not in_target_handler:
            remaining_lines.append(line)

    with open(SOURCE, "w", encoding="utf-8") as f:
        f.write("\n".join(remaining_lines))
        
    print(f"Refactor complete. Commands.py is now {len(remaining_lines)} lines.")

if __name__ == "__main__":
    main()
