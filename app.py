import os
import re
import json
import time
import logging
from pathlib import Path
import shutil
import requests

from mutagen.id3 import ID3NoHeaderError
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from openai import OpenAI
from tqdm import tqdm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("metadata_updater.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==========================================
# HARDCODED CONFIGURATION
# ==========================================

# Directory settings
INPUT_FOLDER = "input"                    # Folder containing your audio files
OUTPUT_FOLDER = "output"                  # Folder where processed files will be saved

# LLM Provider Configuration
# Choose between "openai" or "ollama"
LLM_PROVIDER = "ollama"                   # Options: "openai", "ollama"

# OpenAI Configuration (only used if LLM_PROVIDER = "openai")
OPENAI_API_KEY = "YOUR_OPENAI_API_KEY_HERE"    # Your OpenAI API key
OPENAI_MODEL = "gpt-4o"                        # OpenAI model to use (gpt-4o, gpt-4.1, etc.)

# Ollama Configuration (only used if LLM_PROVIDER = "ollama")
OLLAMA_BASE_URL = "http://localhost:11434"     # Ollama server URL (default: http://localhost:11434)
OLLAMA_MODEL = "gemma3:4b"                     # Ollama model to use (gemma3:4b, llama3.2, etc.)

# Processing settings
BATCH_SIZE = 10                          # Number of files to process before showing progress
RATE_LIMIT_DELAY = 1.0                   # Delay between API calls in seconds
OVERWRITE = True                         # Whether to overwrite existing metadata

class MusicMetadataGenerator:
    def __init__(self):
        """
        Initialize the Music metadata generator for multiple audio formats with hardcoded values.
        """
        self.input_folder = Path(INPUT_FOLDER)
        self.output_folder = Path(OUTPUT_FOLDER)
        self.batch_size = BATCH_SIZE
        self.rate_limit_delay = RATE_LIMIT_DELAY
        self.overwrite = OVERWRITE
        self.llm_provider = LLM_PROVIDER.lower()
        
        # Ensure folders exist
        if not self.input_folder.exists():
            raise FileNotFoundError(f"Input folder not found: {self.input_folder}")
        
        if not self.output_folder.exists():
            logger.info(f"Creating output folder: {self.output_folder}")
            self.output_folder.mkdir(parents=True, exist_ok=True)
            
        # Initialize LLM client based on provider
        self._initialize_llm_client()

        # Statistics for reporting
        self.stats = {
            "total_files": 0,
            "processed_files": 0,
            "success": 0,
            "errors": 0,
            "skipped": 0
        }
    
    def _initialize_llm_client(self):
        """Initialize the appropriate LLM client based on the configured provider."""
        if self.llm_provider == "openai":
            try:
                self.client = OpenAI(api_key=OPENAI_API_KEY)
                self.model = OPENAI_MODEL
                logger.info(f"Initialized OpenAI client with model: {self.model}")
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")
                raise
        elif self.llm_provider == "ollama":
            try:
                self.ollama_url = OLLAMA_BASE_URL.rstrip('/')
                self.model = OLLAMA_MODEL
                # Test Ollama connection
                self._test_ollama_connection()
                logger.info(f"Initialized Ollama client with model: {self.model} at {self.ollama_url}")
            except Exception as e:
                logger.error(f"Failed to initialize Ollama client: {e}")
                raise
        else:
            raise ValueError(f"Unsupported LLM provider: {self.llm_provider}. Use 'openai' or 'ollama'")
    
    def _test_ollama_connection(self):
        """Test connection to Ollama server."""
        try:
            response = requests.get(f"{self.ollama_url}/api/tags", timeout=5)
            if response.status_code != 200:
                raise Exception(f"Ollama server responded with status {response.status_code}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Cannot connect to Ollama server at {self.ollama_url}: {e}")
    
    def _call_ollama_api(self, prompt):
        """Make a request to Ollama API."""
        try:
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "format": "json"
            }
            
            response = requests.post(
                f"{self.ollama_url}/api/generate",
                json=payload,
                timeout=60
            )
            
            if response.status_code != 200:
                raise Exception(f"Ollama API returned status {response.status_code}: {response.text}")
            
            result = response.json()
            return result.get("response", "")
            
        except Exception as e:
            logger.error(f"Error calling Ollama API: {e}")
            raise
        
    def get_audio_files(self):
        """Find all supported audio files in the input folder."""
        try:
            supported_extensions = ['*.mp3', '*.flac', '*.m4a', '*.mp4', '*.ogg', '*.opus']
            audio_files = []
            
            for extension in supported_extensions:
                files = list(self.input_folder.glob(f"**/{extension}"))
                audio_files.extend(files)
            
            logger.info(f"Found {len(audio_files)} audio files in {self.input_folder}")
            logger.info(f"Supported formats: {', '.join([ext.replace('*.', '.') for ext in supported_extensions])}")
            self.stats["total_files"] = len(audio_files)
            return audio_files
        except Exception as e:
            logger.error(f"Error finding audio files: {e}")
            return []
    
    def clean_filename(self, filename):
        """Extract and clean the song name from the filename."""
        # Remove file extension
        name = os.path.splitext(filename)[0]
        
        # Remove common prefixes, numbering, etc.
        name = re.sub(r'^\d+[\s_\-\.]+', '', name)  # Remove leading numbers with separators
        name = re.sub(r'^\[.*?\][\s_\-\.]*', '', name)  # Remove bracketed text at start
        
        # Replace separators with spaces
        name = re.sub(r'[_\-\.]+', ' ', name)
        
        # Remove extra spaces
        name = re.sub(r'\s+', ' ', name).strip()
        
        return name
    
    def get_metadata_from_llm(self, song_name):
        """Query the configured LLM to get metadata for a song."""
        prompt = f"""
        I need detailed metadata for the song titled "{song_name}". 
        
        Please provide the following information:
        - Title: The full and correct title of the song
        - Artists: The performers/singers of the song (as a comma-separated string, not an array)
        - Album: The album name or compilation it's from
        - Year: The release year (as a number)
        - Composer: The composer/producer/music director
        - Genre: The primary genre of the song
        - Language: The language of the song's lyrics (if applicable)
        
        Return your response ONLY as a JSON object with these fields. If you're uncertain about any field, provide your best guess but mark it with "confidence": "low". If you cannot determine a field at all, use null for its value.
        
        Example response format:

        Example 1 (English song):
        {{
          "title": "Yesterday",
          "artists": "The Beatles",
          "album": "Help!",
          "year": 1965,
          "composer": "John Lennon, Paul McCartney",
          "genre": "Rock",
          "language": "English"
        }}

        Example 2 (Hindi song):
        {{
          "title": "Tum Hi Ho",
          "artists": "Arijit Singh",
          "album": "Aashiqui 2",
          "year": 2013,
          "composer": "Mithoon",
          "genre": "Indian Pop",
          "language": "Hindi"
        }}
        """
        
        try:
            if self.llm_provider == "openai":
                return self._get_metadata_from_openai(prompt, song_name)
            elif self.llm_provider == "ollama":
                return self._get_metadata_from_ollama(prompt, song_name)
            else:
                raise ValueError(f"Unsupported LLM provider: {self.llm_provider}")
                
        except Exception as e:
            logger.error(f"Error getting metadata for '{song_name}': {e}")
            return {
                "error": str(e),
                "title": song_name
            }
    
    def _get_metadata_from_openai(self, prompt, song_name):
        """Get metadata using OpenAI API."""
        response = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a music metadata expert with comprehensive knowledge of music across all genres, artists, and time periods. Provide accurate metadata in JSON format ONLY. Do not include any explanations or comments outside the JSON object."},
                {"role": "user", "content": prompt}
            ]
        )
        
        metadata_json = response.choices[0].message.content
        try:
            metadata = json.loads(metadata_json)
            logger.debug(f"Got metadata for '{song_name}': {metadata}")
            return metadata
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from OpenAI response: {e}")
            logger.error(f"Raw response: {metadata_json}")
            return {"title": song_name, "error": "Failed to parse OpenAI response"}
    
    def _get_metadata_from_ollama(self, prompt, song_name):
        """Get metadata using Ollama API."""
        system_prompt = "You are a music metadata expert with comprehensive knowledge of music across all genres, artists, and time periods. Provide accurate metadata in JSON format ONLY. Do not include any explanations or comments outside the JSON object."
        
        full_prompt = f"{system_prompt}\n\n{prompt}"
        
        response_text = self._call_ollama_api(full_prompt)
        
        try:
            metadata = json.loads(response_text)
            logger.debug(f"Got metadata for '{song_name}': {metadata}")
            return metadata
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from Ollama response: {e}")
            logger.error(f"Raw response: {response_text}")
            # Try to extract JSON from the response if it contains other text
            try:
                import re
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    metadata = json.loads(json_match.group())
                    logger.debug(f"Extracted metadata for '{song_name}': {metadata}")
                    return metadata
            except:
                pass
            return {"title": song_name, "error": "Failed to parse Ollama response"}
    
    def update_audio_metadata(self, file_path, metadata):
        """Update the metadata tags of an audio file with the provided metadata."""
        try:
            # Create output path (convert Path to string to avoid issues)
            output_path = str(self.output_folder / file_path.name)
            source_path = str(file_path)
            
            # Copy the file to the output directory
            logger.info(f"Copying file to output folder: {output_path}")
            shutil.copy2(source_path, output_path)
            
            # Load the audio file using mutagen
            audiofile = MutagenFile(output_path)
            
            # Check if the file was loaded properly
            if audiofile is None:
                logger.error(f"Failed to load audio file {output_path}")
                self.stats["errors"] += 1
                return False
            
            file_ext = file_path.suffix.lower()
            
            # Check for existing metadata and skip if overwrite is False
            if not self.overwrite and self._has_existing_metadata(audiofile, file_ext):
                logger.info(f"Skipping '{file_path.name}' - already has metadata and overwrite is False")
                self.stats["skipped"] += 1
                return False
                
            # Update metadata based on file format
            if file_ext == '.mp3':
                success = self._update_mp3_tags(audiofile, metadata)
            elif file_ext == '.flac':
                success = self._update_flac_tags(audiofile, metadata)
            elif file_ext in ['.m4a', '.mp4']:
                success = self._update_mp4_tags(audiofile, metadata)
            elif file_ext == '.ogg':
                success = self._update_ogg_tags(audiofile, metadata)
            elif file_ext == '.opus':
                success = self._update_opus_tags(audiofile, metadata)
            else:
                logger.error(f"Unsupported file format: {file_ext}")
                self.stats["errors"] += 1
                return False
            
            if success:
                # Save the changes
                audiofile.save()
                logger.info(f"Successfully updated metadata for '{file_path.name}' saved to {output_path}")
                self.stats["success"] += 1
                return True
            else:
                self.stats["errors"] += 1
                return False
            
        except Exception as e:
            logger.error(f"Error updating metadata for '{file_path.name}': {str(e)}")
            self.stats["errors"] += 1
            return False
    
    def _has_existing_metadata(self, audiofile, file_ext):
        """Check if the audio file already has metadata."""
        try:
            if file_ext == '.mp3':
                return bool(audiofile.get('TIT2') or audiofile.get('TPE1'))
            elif file_ext == '.flac':
                return bool(audiofile.get('TITLE') or audiofile.get('ARTIST'))
            elif file_ext in ['.m4a', '.mp4']:
                return bool(audiofile.get('\xa9nam') or audiofile.get('\xa9ART'))
            elif file_ext in ['.ogg', '.opus']:
                return bool(audiofile.get('TITLE') or audiofile.get('ARTIST'))
            return False
        except:
            return False
    
    def _update_mp3_tags(self, audiofile, metadata):
        """Update MP3 ID3 tags."""
        try:
            from mutagen.id3 import TIT2, TPE1, TALB, TDRC, TCOM, TCON, COMM
            
            if metadata.get("title"):
                audiofile['TIT2'] = TIT2(encoding=3, text=str(metadata["title"]))
                
            if metadata.get("artists"):
                artists_str = metadata["artists"] if isinstance(metadata["artists"], str) else ", ".join(str(a) for a in metadata["artists"])
                audiofile['TPE1'] = TPE1(encoding=3, text=artists_str)
                    
            if metadata.get("album"):
                audiofile['TALB'] = TALB(encoding=3, text=str(metadata["album"]))
                
            if metadata.get("year"):
                try:
                    year_str = str(metadata["year"])
                    if year_str.isdigit():
                        audiofile['TDRC'] = TDRC(encoding=3, text=year_str)
                except:
                    pass
                
            if metadata.get("composer"):
                audiofile['TCOM'] = TCOM(encoding=3, text=str(metadata["composer"]))
                
            if metadata.get("genre"):
                audiofile['TCON'] = TCON(encoding=3, text=str(metadata["genre"]))
                
            if metadata.get("language"):
                audiofile['COMM'] = COMM(encoding=3, lang='eng', desc='Language', text=str(metadata["language"]))
            
            return True
        except Exception as e:
            logger.error(f"Error updating MP3 tags: {e}")
            return False
    
    def _update_flac_tags(self, audiofile, metadata):
        """Update FLAC vorbis comments."""
        try:
            if metadata.get("title"):
                audiofile['TITLE'] = str(metadata["title"])
                
            if metadata.get("artists"):
                artists_str = metadata["artists"] if isinstance(metadata["artists"], str) else ", ".join(str(a) for a in metadata["artists"])
                audiofile['ARTIST'] = artists_str
                    
            if metadata.get("album"):
                audiofile['ALBUM'] = str(metadata["album"])
                
            if metadata.get("year"):
                try:
                    year_str = str(metadata["year"])
                    if year_str.isdigit():
                        audiofile['DATE'] = year_str
                except:
                    pass
                
            if metadata.get("composer"):
                audiofile['COMPOSER'] = str(metadata["composer"])
                
            if metadata.get("genre"):
                audiofile['GENRE'] = str(metadata["genre"])
                
            if metadata.get("language"):
                audiofile['LANGUAGE'] = str(metadata["language"])
            
            return True
        except Exception as e:
            logger.error(f"Error updating FLAC tags: {e}")
            return False
    
    def _update_mp4_tags(self, audiofile, metadata):
        """Update MP4/M4A tags."""
        try:
            if metadata.get("title"):
                audiofile['\xa9nam'] = str(metadata["title"])
                
            if metadata.get("artists"):
                artists_str = metadata["artists"] if isinstance(metadata["artists"], str) else ", ".join(str(a) for a in metadata["artists"])
                audiofile['\xa9ART'] = artists_str
                    
            if metadata.get("album"):
                audiofile['\xa9alb'] = str(metadata["album"])
                
            if metadata.get("year"):
                try:
                    year_str = str(metadata["year"])
                    if year_str.isdigit():
                        audiofile['\xa9day'] = year_str
                except:
                    pass
                
            if metadata.get("composer"):
                audiofile['\xa9wrt'] = str(metadata["composer"])
                
            if metadata.get("genre"):
                audiofile['\xa9gen'] = str(metadata["genre"])
                
            if metadata.get("language"):
                audiofile['\xa9lyr'] = f"Language: {metadata['language']}"
            
            return True
        except Exception as e:
            logger.error(f"Error updating MP4 tags: {e}")
            return False
    
    def _update_ogg_tags(self, audiofile, metadata):
        """Update OGG Vorbis comments."""
        try:
            if metadata.get("title"):
                audiofile['TITLE'] = str(metadata["title"])
                
            if metadata.get("artists"):
                artists_str = metadata["artists"] if isinstance(metadata["artists"], str) else ", ".join(str(a) for a in metadata["artists"])
                audiofile['ARTIST'] = artists_str
                    
            if metadata.get("album"):
                audiofile['ALBUM'] = str(metadata["album"])
                
            if metadata.get("year"):
                try:
                    year_str = str(metadata["year"])
                    if year_str.isdigit():
                        audiofile['DATE'] = year_str
                except:
                    pass
                
            if metadata.get("composer"):
                audiofile['COMPOSER'] = str(metadata["composer"])
                
            if metadata.get("genre"):
                audiofile['GENRE'] = str(metadata["genre"])
                
            if metadata.get("language"):
                audiofile['LANGUAGE'] = str(metadata["language"])
            
            return True
        except Exception as e:
            logger.error(f"Error updating OGG tags: {e}")
            return False
    
    def _update_opus_tags(self, audiofile, metadata):
        """Update Opus comments."""
        try:
            if metadata.get("title"):
                audiofile['TITLE'] = str(metadata["title"])
                
            if metadata.get("artists"):
                artists_str = metadata["artists"] if isinstance(metadata["artists"], str) else ", ".join(str(a) for a in metadata["artists"])
                audiofile['ARTIST'] = artists_str
                    
            if metadata.get("album"):
                audiofile['ALBUM'] = str(metadata["album"])
                
            if metadata.get("year"):
                try:
                    year_str = str(metadata["year"])
                    if year_str.isdigit():
                        audiofile['DATE'] = year_str
                except:
                    pass
                
            if metadata.get("composer"):
                audiofile['COMPOSER'] = str(metadata["composer"])
                
            if metadata.get("genre"):
                audiofile['GENRE'] = str(metadata["genre"])
                
            if metadata.get("language"):
                audiofile['LANGUAGE'] = str(metadata["language"])
            
            return True
        except Exception as e:
            logger.error(f"Error updating Opus tags: {e}")
            return False

    def process_files(self):
        """Process all audio files in the input folder."""
        audio_files = self.get_audio_files()
        if not audio_files:
            logger.warning("No audio files found to process.")
            return
            
        logger.info(f"Starting to process {len(audio_files)} files...")
        
        for i, file_path in enumerate(tqdm(audio_files, desc="Processing audio files")):
            try:
                # Extract song name from filename
                song_name = self.clean_filename(file_path.name)
                logger.info(f"Processing ({i+1}/{len(audio_files)}): '{song_name}' [{file_path.suffix.upper()}]")
                
                # Get metadata from LLM
                metadata = self.get_metadata_from_llm(song_name)
                
                # Update audio file with metadata
                success = self.update_audio_metadata(file_path, metadata)
                
                if not success and "error" in metadata:
                    logger.error(f"Failed to update metadata: {metadata.get('error')}")
                
                # Update processed count regardless of success/failure
                self.stats["processed_files"] += 1
                
                # Print batch status
                if (i + 1) % self.batch_size == 0 or i == len(audio_files) - 1:
                    logger.info(f"Progress: {i+1}/{len(audio_files)} files processed.")
                    
                # Delay to avoid rate limiting
                if i < len(audio_files) - 1:  # No need to delay after the last file
                    time.sleep(self.rate_limit_delay)
                    
            except Exception as e:
                logger.error(f"Error processing file '{file_path}': {str(e)}")
                self.stats["errors"] += 1
                continue
    
    def print_summary(self):
        """Print a summary of the processing results."""
        logger.info("\n" + "="*50)
        logger.info("PROCESSING SUMMARY")
        logger.info("="*50)
        logger.info(f"Total files found: {self.stats['total_files']}")
        logger.info(f"Files processed: {self.stats['processed_files']}")
        logger.info(f"Successful updates: {self.stats['success']}")
        logger.info(f"Skipped files: {self.stats['skipped']}")
        logger.info(f"Errors: {self.stats['errors']}")
        logger.info("="*50 + "\n")


def main():
    try:
        print("Starting Music Metadata Generator (Multi-Format Support)")
        print(f"Input folder: {INPUT_FOLDER}")
        print(f"Output folder: {OUTPUT_FOLDER}")
        print("Supported formats: MP3, FLAC, M4A, MP4, OGG, OPUS")
        print(f"LLM Provider: {LLM_PROVIDER.upper()}")
        
        if LLM_PROVIDER.lower() == "openai":
            print(f"OpenAI Model: {OPENAI_MODEL}")
        elif LLM_PROVIDER.lower() == "ollama":
            print(f"Ollama Model: {OLLAMA_MODEL}")
            print(f"Ollama URL: {OLLAMA_BASE_URL}")
        
        generator = MusicMetadataGenerator()
        generator.process_files()
        generator.print_summary()
        
        print("\nProcess completed! Check metadata_updater.log for details.")
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        print(f"Fatal error occurred: {e}")
        return 1
        
    return 0


if __name__ == "__main__":
    exit(main())