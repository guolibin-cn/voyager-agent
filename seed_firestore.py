from google.cloud import firestore

# IMPORTANT: Project ID must be hardcoded as a string (not read from env / google.auth.default)
FIRESTORE_PROJECT_ID = "qwiklabs-gcp-01-8abcae82c3b6"


def seed_destinations():
    db = firestore.Client(project=FIRESTORE_PROJECT_ID)
    destinations_ref = db.collection("destinations")

    sample_destinations = [
        {
            "id": "kyoto-japan",
            "name": "Kyoto, Japan",
            "category": "Cultural & Historic",
            "description": "Historic city famed for classical Buddhist temples, gardens, imperial palaces, and traditional wooden houses.",
            "best_season": "Spring (March-May) & Autumn (Sept-Nov)",
            "avg_daily_cost_usd": 180,
            "rating": 4.9,
            "highlights": [
                "Fushimi Inari Shrine",
                "Arashiyama Bamboo Grove",
                "Gion Geisha District",
                "Kinkaku-ji (Golden Pavilion)",
            ],
        },
        {
            "id": "paris-france",
            "name": "Paris, France",
            "category": "Urban & Culinary",
            "description": "Global center for art, fashion, gastronomy, and culture, known for romantic landmarks and world-class museums.",
            "best_season": "Spring (April-May) & Autumn (Sept-Oct)",
            "avg_daily_cost_usd": 220,
            "rating": 4.8,
            "highlights": [
                "Eiffel Tower",
                "Louvre Museum",
                "Notre-Dame Cathedral",
                "Montmartre & Sacré-Cœur",
            ],
        },
        {
            "id": "bali-indonesia",
            "name": "Bali, Indonesia",
            "category": "Tropical & Relaxation",
            "description": "Indonesian island known for iconic rice paddies, volcanic mountains, coral reefs, and vibrant beach culture.",
            "best_season": "Dry Season (April-October)",
            "avg_daily_cost_usd": 90,
            "rating": 4.7,
            "highlights": [
                "Ubud Monkey Forest",
                "Tegallalang Rice Terraces",
                "Uluwatu Temple",
                "Seminyak Beach",
            ],
        },
        {
            "id": "reykjavik-iceland",
            "name": "Reykjavik, Iceland",
            "category": "Nature & Adventure",
            "description": "Gateway to Iceland's dramatic volcanic landscape, geothermal hot springs, waterfalls, and northern lights.",
            "best_season": "Summer (June-August) for hiking / Winter for Northern Lights",
            "avg_daily_cost_usd": 250,
            "rating": 4.8,
            "highlights": [
                "Blue Lagoon",
                "Golden Circle Route",
                "Hallgrímskirkja",
                "Northern Lights Tour",
            ],
        },
    ]

    for item in sample_destinations:
        dest_id = item["id"]
        destinations_ref.document(dest_id).set(item)
        print(f"Seeded destination: {item['name']} ({dest_id})")

    print("\n✅ Successfully seeded Firestore 'destinations' collection!")


if __name__ == "__main__":
    seed_destinations()
