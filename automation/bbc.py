from google import genai

# 1. Initialize the client with your key
# Replace the string below with your actual API key
client = genai.Client(api_key="AIzaSyA_OIDs9GFe9NgnzSwVzu0HUPsQOj7N8ws")

try:
    # 2. Send a simple test message
    print("Connecting to Gemini...")
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite", 
        contents="Say: 'Connection successful! Ready for automation.I love you , Muaaz'"
    )

    # 3. Print the response
    print("-" * 30)
    print("AI Response:", response.text)
    print("-" * 30)

except Exception as e:
    print(f"An error occurred: {e}")