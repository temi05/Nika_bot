import os

SOURCE = "app/bot/commands.py"
DEST = "app/bot/routers/games.py"

def main():
    with open(SOURCE, "r", encoding="utf-8") as f:
        content = f.read()

    commands_to_move = [
        "casino", "dice", "diceduel", "coin", "rps", "duel", "tower", "box", "scratch", "mine", "dailyquest", "fish"
    ]
    
    lines = content.split("\n")
    
    output_lines = []
    remaining_lines = []
    
    in_target_handler = False
    
    # We also want to extract specific globals:
    # DUEL_SESSIONS, DICE_DUEL_SESSIONS, _tower_locks, _tower_sessions, etc.
    # Luckily, most of them like _tower_sessions are right before their handlers.
    
    for line in lines:
        if line.strip().startswith("@router.") or line.strip().startswith("def _get_tower") or line.strip().startswith("def _format_tower") or line.strip().startswith("def _max_game_bet") or line.strip().startswith("async def _deny_bad_bet"):
            is_target = False
            for cmd in commands_to_move:
                if f'Command("{cmd}"' in line or f'Command("{cmd}",' in line:
                    is_target = True
                    break
            
            # Special helpers for games
            if any(line.strip().startswith(prefix) for prefix in [
                "def _get_tower", "def _format_tower", "async def _deny_bad_bet", 
                "def _max_game_bet", "def _risk_keyboard"
            ]):
                is_target = True
                
            in_target_handler = is_target

        if in_target_handler or line.strip() in [
            "DUEL_SESSIONS: dict[str, dict] = {}",
            "DICE_DUEL_SESSIONS: dict[str, dict] = {}",
            "_tower_locks = {}",
            "_tower_sessions = {}"
        ]:
            # Actually, globals might be at the top level and not inside the loop state
            # I will just grab the functions first and copy globals manually to the template.
            pass

        if in_target_handler:
            output_lines.append(line)
        else:
            remaining_lines.append(line)

    with open("scratch/games_raw.py", "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines))
        
    print(f"Extracted {len(output_lines)} lines for games.")

    with open(SOURCE, "w", encoding="utf-8") as f:
        f.write("\n".join(remaining_lines))

if __name__ == "__main__":
    main()
