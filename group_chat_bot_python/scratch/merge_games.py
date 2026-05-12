import os

RAW = "scratch/games_raw.py"
DEST = "app/bot/routers/games.py"

def main():
    with open(RAW, "r", encoding="utf-8") as f:
        raw_lines = f.read()

    with open(DEST, "r", encoding="utf-8") as f:
        dest_content = f.read()
        
    final_dest = dest_content.replace("    # We will insert the raw lines here", raw_lines)
    with open(DEST, "w", encoding="utf-8") as f:
        f.write(final_dest)

    print("Games merge complete.")

if __name__ == "__main__":
    main()
