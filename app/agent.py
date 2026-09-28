# ruff: noqa
import datetime
import json
import os
import pathlib
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from app.a2ui_utils import a2ui_callback
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors.agent_engine_sandbox_code_executor import (
    AgentEngineSandboxCodeExecutor,
)
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
import google.genai as genai
from google.genai import types

load_dotenv()

MODEL = "gemini-2.5-flash"

# IMPORTANT: Hardcoded project ID, Cloud Storage bucket, and Memory Bank ID constants
FIRESTORE_PROJECT_ID = "qwiklabs-gcp-01-8abcae82c3b6"
GCS_BUCKET_NAME = "voyager-agent-assets-qwiklabs-gcp-01-8abcae82c3b6"
MEMORY_BANK_ID = "8708121096869642240"

# Load Agent Engine resource name from deployment_metadata.json for sandbox code execution
METADATA_PATH = pathlib.Path(__file__).parent.parent / "deployment_metadata.json"
AGENT_ENGINE_RESOURCE_NAME = None

if METADATA_PATH.exists():
    try:
        with open(METADATA_PATH, "r") as f:
            metadata = json.load(f)
            AGENT_ENGINE_RESOURCE_NAME = metadata.get("remote_agent_runtime_id")
    except Exception:
        pass

code_executor = None
if AGENT_ENGINE_RESOURCE_NAME:
    code_executor = AgentEngineSandboxCodeExecutor(
        agent_engine_resource_name=AGENT_ENGINE_RESOURCE_NAME
    )

# Build system prompt using A2uiSchemaManager (version 0.8) and BasicCatalog
schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

instruction = schema_manager.generate_system_prompt(
    role_description="You are Voyager, a helpful Travel & Itinerary Concierge agent. You remember the user's stated preferences, food allergies, dietary restrictions, and health requirements from previous conversations to personalize responses. Always check for and strictly adhere to user allergies when recommending dining, activities, or destinations. You can run Python code in a secure sandbox using execute_code and use your tools (search_destinations, add_destination, calculate_itinerary_budget, get_live_weather, geocode_address, find_nearby_places, generate_destination_image, generate_destination_video) to help users explore destinations, geocode addresses, find attractions, plan budgets, generate visual travel postcards, and create short destination preview videos.",
    workflow_description="Analyze the request and return structured UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        '{"Image": {"url": {"literalString": "https://..."}}}. Never point an '
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)


async def generate_memories_callback(callback_context: CallbackContext):
    """Callback triggered after each agent turn to extract and persist long-term memories."""
    try:
        await callback_context.add_session_to_memory()
    except ValueError:
        pass
    return None


def memory_bank_service_builder():
    """Builds VertexAiMemoryBankService for deployed agent runtime."""
    return VertexAiMemoryBankService(
        project=FIRESTORE_PROJECT_ID,
        location="us-east1",
        agent_engine_id=MEMORY_BANK_ID,
    )


def get_firestore_client() -> firestore.Client:
    return firestore.Client(project=FIRESTORE_PROJECT_ID)


def search_destinations(query: str = "") -> list[dict]:
    """Search travel destinations stored in the Firestore database.

    Args:
        query: Optional search keyword to filter destinations by name, category, or description. If empty or 'all', returns all destinations.

    Returns:
        A list of destination dictionaries matching the search criteria.
    """
    db = get_firestore_client()
    docs = db.collection("destinations").stream()
    results = []
    q_lower = query.strip().lower()

    for doc in docs:
        data = doc.to_dict()
        if not q_lower or q_lower == "all":
            results.append(data)
        else:
            name = str(data.get("name", "")).lower()
            cat = str(data.get("category", "")).lower()
            desc = str(data.get("description", "")).lower()
            if q_lower in name or q_lower in cat or q_lower in desc:
                results.append(data)

    return results


def add_destination(
    dest_id: str,
    name: str,
    category: str,
    description: str,
    best_season: str,
    avg_daily_cost_usd: float,
    highlights: str,
) -> str:
    """Add a new travel destination to the Firestore database.

    Args:
        dest_id: Unique slug identifier for the destination (e.g. 'tokyo-japan').
        name: Name of the destination (e.g. 'Tokyo, Japan').
        category: Category of travel (e.g. 'Cultural', 'Adventure', 'Relaxation').
        description: A brief summary description of the location.
        best_season: Best season or time of year to visit.
        avg_daily_cost_usd: Estimated average daily cost in USD.
        highlights: Comma-separated list of top attraction highlights.

    Returns:
        A confirmation message indicating success.
    """
    db = get_firestore_client()
    highlight_list = [h.strip() for h in highlights.split(",") if h.strip()]
    doc_data = {
        "id": dest_id,
        "name": name,
        "category": category,
        "description": description,
        "best_season": best_season,
        "avg_daily_cost_usd": avg_daily_cost_usd,
        "highlights": highlight_list,
    }
    db.collection("destinations").document(dest_id).set(doc_data)
    return f"Successfully added destination '{name}' ({dest_id}) to Firestore."


def calculate_itinerary_budget(
    destination_id: str, days: int = 3, travelers: int = 1
) -> dict:
    """Calculate an estimated budget for a trip to a specified destination based on Firestore rates.

    Args:
        destination_id: Unique slug identifier of the destination (e.g. 'kyoto-japan', 'paris-france', 'bali-indonesia').
        days: Number of trip days (default is 3).
        travelers: Number of travelers in the party (default is 1).

    Returns:
        A dictionary containing cost breakdown (lodging, dining, activities, contingency) and total costs.
    """
    db = get_firestore_client()
    doc_ref = db.collection("destinations").document(destination_id.lower().strip())
    doc = doc_ref.get()

    if not doc.exists:
        return {"error": f"Destination '{destination_id}' not found in database."}

    data = doc.to_dict()
    avg_daily = float(data.get("avg_daily_cost_usd", 150))
    base_cost = avg_daily * days * travelers

    lodging = round(base_cost * 0.45, 2)
    dining = round(base_cost * 0.30, 2)
    activities = round(base_cost * 0.15, 2)
    contingency = round(base_cost * 0.10, 2)
    total_cost = round(lodging + dining + activities + contingency, 2)
    per_person = round(total_cost / max(1, travelers), 2)

    return {
        "destination": data.get("name", destination_id),
        "days": days,
        "travelers": travelers,
        "cost_breakdown_usd": {
            "lodging": lodging,
            "dining": dining,
            "activities": activities,
            "contingency_buffer": contingency,
        },
        "total_estimated_cost_usd": total_cost,
        "per_person_cost_usd": per_person,
    }


def get_live_weather(city: str) -> dict:
    """Fetch real-time live weather forecasts and current conditions for any destination worldwide using Open-Meteo API.

    Args:
        city: Destination or city name (e.g. 'Kyoto', 'Paris', 'Bali', 'Reykjavik', 'Cairo').

    Returns:
        A dictionary containing live weather temperature, windspeed, and forecast high/low temperatures.
    """
    try:
        encoded_city = urllib.parse.quote(city.strip())
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_city}&count=1&language=en&format=json"
        req = urllib.request.Request(
            geo_url, headers={"User-Agent": "VoyagerAgent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            geo_data = json.loads(resp.read().decode())

        if not geo_data.get("results"):
            return {"error": f"Could not find coordinates for location '{city}'."}

        loc = geo_data["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]

        weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&daily=temperature_2m_max,temperature_2m_min&timezone=auto"
        req_w = urllib.request.Request(
            weather_url, headers={"User-Agent": "VoyagerAgent/1.0"}
        )
        with urllib.request.urlopen(req_w, timeout=5) as resp_w:
            w_data = json.loads(resp_w.read().decode())

        current = w_data.get("current_weather", {})
        daily = w_data.get("daily", {})

        return {
            "city": loc.get("name", city),
            "country": loc.get("country", ""),
            "current_temperature_celsius": current.get("temperature"),
            "windspeed_kmh": current.get("windspeed"),
            "daily_max_celsius": daily.get("temperature_2m_max", [None])[0],
            "daily_min_celsius": daily.get("temperature_2m_min", [None])[0],
            "unit": "Celsius",
        }
    except Exception as e:
        return {"error": f"Failed to fetch live weather for '{city}': {str(e)}"}


def geocode_address(address: str) -> dict:
    """Turn an address or location name into geographic coordinates using Google Maps Geocoding API.

    Args:
        address: The street address, landmark, or city name to geocode (e.g. 'Kyoto Station' or 'Eiffel Tower, Paris').

    Returns:
        A dictionary with formatted address, latitude, longitude, and place_id.
    """
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return {"error": "GOOGLE_MAPS_API_KEY environment variable is not set."}

    try:
        encoded_addr = urllib.parse.quote(address.strip())
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={encoded_addr}&key={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())

        if data.get("status") != "OK" or not data.get("results"):
            return {
                "error": f"Geocoding failed for '{address}': {data.get('status', 'ZERO_RESULTS')}"
            }

        first_res = data["results"][0]
        loc = first_res["geometry"]["location"]
        return {
            "name": address,
            "address": first_res.get("formatted_address"),
            "location": {"latitude": loc["lat"], "longitude": loc["lng"]},
            "place_id": first_res.get("place_id"),
        }
    except Exception as e:
        return {"error": f"Geocoding request error: {str(e)}"}


def find_nearby_places(
    latitude: float,
    longitude: float,
    place_type: str = "tourist_attraction",
    radius_meters: float = 5000.0,
) -> list[dict]:
    """Find nearby points of interest or places around coordinates using Google Places API (New).

    Args:
        latitude: Center latitude coordinate.
        longitude: Center longitude coordinate.
        place_type: Type of place to search for (e.g. 'tourist_attraction', 'restaurant', 'museum', 'lodging', 'park').
        radius_meters: Search radius in meters (default 5000.0).

    Returns:
        A list of nearby places with name, formatted address, location coordinates, and rating.
    """
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return [{"error": "GOOGLE_MAPS_API_KEY environment variable is not set."}]

    try:
        url = "https://places.googleapis.com/v1/places:searchNearby"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location,places.rating",
        }
        body = json.dumps({
            "includedTypes": [place_type],
            "locationRestriction": {
                "circle": {
                    "center": {
                        "latitude": float(latitude),
                        "longitude": float(longitude),
                    },
                    "radius": float(radius_meters),
                }
            },
        }).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())

        places = data.get("places", [])
        results = []
        for p in places[:10]:
            display = p.get("displayName", {})
            results.append({
                "name": display.get("text", "Unknown"),
                "address": p.get("formattedAddress"),
                "location": p.get("location"),
                "rating": p.get("rating"),
            })
        return results
    except Exception as e:
        return [{"error": f"Places searchNearby request error: {str(e)}"}]


def generate_destination_image(
    destination_name: str, tool_context: ToolContext = None
) -> dict:
    """Generate a travel postcard/photo for a destination using gemini-3.1-flash-lite-image in global region.

    Saves the image as a Playground Artifact and uploads it directly to public Cloud Storage, returning the public HTTPS URL.

    Args:
        destination_name: Name of the destination (e.g. 'Kyoto, Japan' or 'Eiffel Tower, Paris').

    Returns:
        A dictionary containing destination name, public GCS HTTPS URL, and artifact status.
    """
    try:
        client = genai.Client(
            vertexai=True, project=FIRESTORE_PROJECT_ID, location="global"
        )
        prompt = f"A beautiful high-quality travel postcard photo of {destination_name}"
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=prompt,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )

        if not response.candidates or not response.candidates[0].content.parts:
            return {"error": "No image generated."}

        part = response.candidates[0].content.parts[0]
        if not hasattr(part, "inline_data") or not part.inline_data:
            return {"error": "Returned part does not contain image data."}

        image_bytes = part.inline_data.data
        mime_type = part.inline_data.mime_type or "image/jpeg"

        safe_name = "".join(
            c if c.isalnum() else "_" for c in destination_name.lower().strip()
        )
        filename = f"{safe_name}_postcard.jpg"

        # 1. Save with tool_context.save_artifact for Playground Artifacts panel
        artifact_saved = False
        if tool_context is not None:
            artifact_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            tool_context.save_artifact(filename=filename, artifact=artifact_part)
            artifact_saved = True

        # 2. Upload image bytes to public Cloud Storage bucket and return public https URL
        storage_client = storage.Client(project=FIRESTORE_PROJECT_ID)
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(image_bytes, content_type=mime_type)
        public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{filename}"

        return {
            "destination": destination_name,
            "public_url": public_url,
            "artifact_saved": artifact_saved,
            "filename": filename,
        }
    except Exception as e:
        return {"error": f"Failed to generate image: {str(e)}"}


def generate_destination_video(
    destination_name: str, tool_context: ToolContext = None
) -> dict:
    """Generate a short travel video preview for a destination using gemini-omni-flash-preview in global region.

    Saves the video as a Playground Artifact and uploads it directly to public Cloud Storage, returning the public HTTPS URL.

    Args:
        destination_name: Name of the destination or travel attraction (e.g. 'Kyoto, Japan' or 'Eiffel Tower, Paris').

    Returns:
        A dictionary containing destination name, public GCS HTTPS URL, and artifact status.
    """
    try:
        client = genai.Client(
            vertexai=True, project=FIRESTORE_PROJECT_ID, location="global"
        )
        prompt = f"A short 3-second travel video clip of {destination_name} highlighting its scenery and atmosphere"

        video_bytes = None
        mime_type = "video/mp4"

        try:
            interaction = client.interactions.create(
                model="gemini-omni-flash-preview",
                input=prompt,
                generation_config={"response_modalities": ["VIDEO"]},
            )
            if interaction.output_video and interaction.output_video.data:
                data = interaction.output_video.data
                if isinstance(data, bytes):
                    video_bytes = data
                elif isinstance(data, str):
                    import base64
                    try:
                        video_bytes = base64.b64decode(data)
                    except Exception:
                        video_bytes = data.encode("utf-8")
                if interaction.output_video.mime_type:
                    mime_type = interaction.output_video.mime_type
        except Exception:
            pass

        if not video_bytes:
            try:
                response = client.models.generate_content(
                    model="gemini-omni-flash-preview",
                    contents=prompt,
                    config=types.GenerateContentConfig(response_modalities=["VIDEO"]),
                )
                if response.candidates and response.candidates[0].content.parts:
                    part = response.candidates[0].content.parts[0]
                    if hasattr(part, "inline_data") and part.inline_data:
                        video_bytes = part.inline_data.data
                        mime_type = part.inline_data.mime_type or mime_type
            except Exception:
                pass

        if not video_bytes:
            return {"error": f"Could not generate video for '{destination_name}'."}

        safe_name = "".join(
            c if c.isalnum() else "_" for c in destination_name.lower().strip()
        )
        filename = f"{safe_name}_preview.mp4"

        # 1. Save with tool_context.save_artifact so it shows up in Playground Artifacts panel
        artifact_saved = False
        if tool_context is not None:
            artifact_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
            tool_context.save_artifact(filename=filename, artifact=artifact_part)
            artifact_saved = True

        # 2. Upload video bytes to public Cloud Storage bucket and return public https URL
        storage_client = storage.Client(project=FIRESTORE_PROJECT_ID)
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(video_bytes, content_type=mime_type)
        public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{filename}"

        return {
            "destination": destination_name,
            "public_url": public_url,
            "artifact_saved": artifact_saved,
            "filename": filename,
        }
    except Exception as e:
        return {"error": f"Failed to generate video: {str(e)}"}


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        query: The name of the city to get the current time for.

    Returns:
        A string with the current time information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return (
        f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"
    )


tools_list = [
    search_destinations,
    add_destination,
    calculate_itinerary_budget,
    get_live_weather,
    geocode_address,
    find_nearby_places,
    generate_destination_image,
    generate_destination_video,
    get_current_time,
    PreloadMemoryTool(),
]

root_agent = Agent(
    name="simple_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=instruction,
    tools=tools_list,
    code_executor=code_executor,
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)

