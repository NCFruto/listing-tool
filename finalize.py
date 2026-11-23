import argparse
import shutil
import sys
from pathlib import Path


def parse_keep(keep_str):
    nums = set()
    for part in keep_str.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            nums.update(range(int(lo), int(hi) + 1))
        else:
            nums.add(int(part))
    return nums


def main():
    parser = argparse.ArgumentParser(
        description="Copy selected photos into a 'selected/' subfolder."
    )
    parser.add_argument("listing_id", help="Listing ID (e.g. 150837108)")
    parser.add_argument(
        "--keep",
        required=True,
        help="Comma-separated photo numbers to keep, e.g. 1,3,5-8",
    )
    args = parser.parse_args()

    base_dir = Path("listings") / args.listing_id
    photos_dir = base_dir / "photos"
    selected_dir = base_dir / "selected"

    if not photos_dir.exists():
        print(f"Error: {photos_dir} does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        keep = parse_keep(args.keep)
    except ValueError:
        print("Error: --keep must be comma-separated numbers or ranges, e.g. 1,3,5-8", file=sys.stderr)
        sys.exit(1)

    selected_dir.mkdir(exist_ok=True)

    # Clear any previous selection
    for f in selected_dir.glob("*.jpg"):
        f.unlink()

    photo_files = sorted(photos_dir.glob("*.jpg"))
    copied = 0

    for photo_path in photo_files:
        num = int(photo_path.stem)
        if num in keep:
            dest = selected_dir / f"{copied + 1:02d}.jpg"
            shutil.copy2(photo_path, dest)
            copied += 1
            print(f"  Kept   {photo_path.name}  ->  selected/{dest.name}")
        else:
            print(f"  Skipped {photo_path.name}")

    print(f"\nDone. {copied} photo(s) saved to listings/{args.listing_id}/selected/")


if __name__ == "__main__":
    main()
