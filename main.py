import os
import re
import random
from google.oauth2.credentials import Credentials
from google.cloud import texttospeech
from pydub import AudioSegment
from pydub.playback import play
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.cloud import translate_v2 as translate

# see https://cloud.google.com/text-to-speech/docs/voices

# SOURCE_LANGUAGE = 'sr-RS'
SOURCE_LANGUAGE = 'pt-PT'
# VOICE_NAME = 'sr-RS-Standard-A'
VOICE_NAME = 'pt-PT-Wavenet-A'

# Set the scope for the Text-to-Speech API
SCOPES = ['https://www.googleapis.com/auth/cloud-platform']

# Set the path to the client ID and secret JSON file that you downloaded
CLIENT_SECRET_FILE = 'client_secret.json'

# If modifying these scopes, delete the file token.json.
creds = None

# The file token.json stores the user's access and refresh tokens, and is
# created automatically when the authorization flow completes for the first
# time.
if os.path.exists('token.json'):
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)

# If there are no (valid) credentials available, let the user log in.
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            CLIENT_SECRET_FILE, SCOPES)
        creds = flow.run_local_server(port=0)

    # Save the credentials for the next run
    with open('token.json', 'w') as token:
        token.write(creds.to_json())

# set up text-to-speech client using credentials
speechClient = texttospeech.TextToSpeechClient(credentials=creds)
translateClient = translate.Client(credentials=creds)

# set voice parameters
voice = texttospeech.VoiceSelectionParams(
    language_code=SOURCE_LANGUAGE,
    name=VOICE_NAME,
    ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
)

# set audio parameters
audio_config = texttospeech.AudioConfig(
    audio_encoding=texttospeech.AudioEncoding.MP3
)


def escape_filename(filename):
    filename = re.sub(r'[\W/]+', '_', filename).strip('_')[:255]
    return filename


to_shuffle = []

with open('phrases.txt', 'r') as f:
    with open('translated.txt', 'w') as tr:
        for phrase in f:
            phrase = phrase.strip()
            if not phrase:
                continue

            translation = translateClient.translate(phrase, source_language=SOURCE_LANGUAGE,
                                                    target_language='ru')
            print(translation)
            import_str = phrase.capitalize() + "\t" + \
                translation['translatedText'].capitalize()
            tr.write(import_str + "\n")
            to_shuffle.append(import_str)

            synthesis_input = texttospeech.SynthesisInput(
                ssml='<speak>' + phrase + '</speak>')
            response = speechClient.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            filename = 'output/' + escape_filename(phrase) + '.mp3'
            with open(filename, 'wb') as out:
                out.write(response.audio_content)
                print(f'Audio content written to file "{filename}"')
            sound = AudioSegment.from_file(filename, format='mp3')
            play(sound)

random.shuffle(to_shuffle)

with open('shuffled.txt', 'w') as file:
    file.writelines(to_shuffle)
