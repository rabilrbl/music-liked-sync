from music_liked_sync.utils import normalize_text, normalize_key
from music_liked_sync.models import Track

def test_normalization_variations():
    t1_text = "You And Me"
    t2_text = "U and Me"
    
    print(f"'{t1_text}' -> '{normalize_text(t1_text)}'")
    print(f"'{t2_text}' -> '{normalize_text(t2_text)}'")
    
    k1 = normalize_key(t1_text, ["Artist"])
    k2 = normalize_key(t2_text, ["Artist"])
    
    print(f"Key 1: {k1}")
    print(f"Key 2: {k2}")
    
    if k1 == k2:
        print("SUCCESS: Keys match")
    else:
        print("FAILURE: Keys do not match")

if __name__ == "__main__":
    test_normalization_variations()
