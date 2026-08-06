import os
import logging
from google.cloud import videointelligence_v1 as videointelligence

logger = logging.getLogger(__name__)

class VideoIntelligenceHelper:
    def __init__(self):
        self.enabled = os.getenv("ENABLE_VIDEO_INTELLIGENCE", "false").lower() == "true"
        self.credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        self.client = None
        
        if self.enabled:
            if not self.credentials_path or not os.path.exists(self.credentials_path):
                logger.warning(f"Video intelligence is enabled but credentials file not found at {self.credentials_path}")
                self.enabled = False
            else:
                try:
                    self.client = videointelligence.VideoIntelligenceServiceClient()
                    logger.info("Successfully initialized VideoIntelligenceServiceClient")
                except Exception as e:
                    logger.error(f"Failed to initialize VideoIntelligenceServiceClient: {e}")
                    self.enabled = False

    def is_enabled(self):
        return self.enabled

    def annotate_video_file(self, file_content: bytes) -> str:
        """
        Analyzes a video file and extracts labels, text, shot changes, explicit content, and speech.
        Returns a formatted string containing the structured video summary.
        """
        if not self.enabled or not self.client:
            return "Video Intelligence API is not enabled."

        try:
            features = [
                videointelligence.Feature.LABEL_DETECTION,
                videointelligence.Feature.TEXT_DETECTION,
                videointelligence.Feature.SHOT_CHANGE_DETECTION,
                videointelligence.Feature.EXPLICIT_CONTENT_DETECTION,
                videointelligence.Feature.SPEECH_TRANSCRIPTION,
            ]

            video_context = videointelligence.VideoContext(
                speech_transcription_config=videointelligence.SpeechTranscriptionConfig(
                    language_code="en-US",
                    enable_automatic_punctuation=True,
                )
            )

            request = videointelligence.AnnotateVideoRequest(
                input_content=file_content,
                features=features,
                video_context=video_context,
            )

            logger.info("Sending video annotation request to Google Cloud...")
            operation = self.client.annotate_video(request)

            logger.info("Waiting for video annotation operation to complete...")
            result = operation.result(timeout=600)  # Wait up to 10 minutes

            if not result.annotation_results:
                return "No annotation results returned."
                
            annotation_result = result.annotation_results[0]
            summary = self._format_results(annotation_result)
            return summary

        except Exception as e:
            logger.error(f"Error during video annotation: {e}")
            return f"Error during video annotation: {str(e)}"

    def _format_results(self, annotation_result) -> str:
        summary_lines = ["### Video Intelligence Analysis Report ###\n"]

        # 1. Shot Changes
        if annotation_result.shot_annotations:
            summary_lines.append("#### Shots ####")
            for i, shot in enumerate(annotation_result.shot_annotations):
                start_time = shot.start_time_offset.total_seconds()
                end_time = shot.end_time_offset.total_seconds()
                summary_lines.append(f"Shot {i + 1}: {start_time:.1f}s to {end_time:.1f}s")
            summary_lines.append("")

        # 2. Segment Labels (overall labels)
        if annotation_result.segment_label_annotations:
            summary_lines.append("#### Overall Labels ####")
            labels = []
            for label_annotation in annotation_result.segment_label_annotations:
                entity_desc = label_annotation.entity.description
                if label_annotation.segments:
                    confidence = label_annotation.segments[0].confidence
                    if confidence > 0.5:
                        labels.append(f"{entity_desc} (confidence: {confidence:.2f})")
            if labels:
                summary_lines.append(", ".join(labels))
            summary_lines.append("")

        # 3. Explicit Content
        if annotation_result.explicit_annotation:
            summary_lines.append("#### Explicit Content Detection ####")
            for frame in annotation_result.explicit_annotation.frames:
                time_offset = frame.time_offset.total_seconds()
                porn_likelihood = videointelligence.Likelihood(frame.pornography_likelihood).name
                if frame.pornography_likelihood > videointelligence.Likelihood.UNLIKELY:
                    summary_lines.append(f"At {time_offset:.1f}s: Pornography likelihood is {porn_likelihood}")
            summary_lines.append("")

        # 4. OCR (Text Detection)
        if annotation_result.text_annotations:
            summary_lines.append("#### Text Detected (OCR) ####")
            for text_annotation in annotation_result.text_annotations:
                text = text_annotation.text
                if text_annotation.segments:
                    start_time = text_annotation.segments[0].segment.start_time_offset.total_seconds()
                    end_time = text_annotation.segments[0].segment.end_time_offset.total_seconds()
                    summary_lines.append(f"Text: '{text}' (Visible from {start_time:.1f}s to {end_time:.1f}s)")
            summary_lines.append("")

        # 5. Speech Transcription
        if annotation_result.speech_transcriptions:
            summary_lines.append("#### Speech Transcription ####")
            for transcription in annotation_result.speech_transcriptions:
                if transcription.alternatives:
                    best_alternative = transcription.alternatives[0]
                    transcript_text = best_alternative.transcript
                    summary_lines.append(f"Speech: '{transcript_text}'")
            summary_lines.append("")

        if len(summary_lines) == 1:
            return "Video Intelligence API returned empty results for all features."
            
        return "\n".join(summary_lines)

    def transcribe_audio_file(self, audio_file_path: str) -> str:
        """
        Converts an audio file to a blank video and extracts speech transcription using Video Intelligence API.
        Returns the raw transcribed text.
        """
        if not self.enabled or not self.client:
            raise Exception("Video Intelligence API is not enabled.")

        import subprocess
        from uuid import uuid4
        
        video_filename = f"temp_{uuid4().hex}.mp4"
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "lavfi",
                "-i", "color=c=black:s=128x128:r=1",
                "-i", audio_file_path,
                "-c:v", "libx264",
                "-tune", "stillimage",
                "-c:a", "aac",
                "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                "-shortest",
                video_filename
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            with open(video_filename, 'rb') as f:
                file_content = f.read()
                
            features = [
                videointelligence.Feature.SPEECH_TRANSCRIPTION,
            ]
            
            video_context = videointelligence.VideoContext(
                speech_transcription_config=videointelligence.SpeechTranscriptionConfig(
                    language_code="en-US",
                    enable_automatic_punctuation=True,
                )
            )

            request = videointelligence.AnnotateVideoRequest(
                input_content=file_content,
                features=features,
                video_context=video_context,
            )

            logger.info("Sending audio (as video) transcription request to Google Cloud...")
            operation = self.client.annotate_video(request)
            result = operation.result(timeout=600)
            
            if not result.annotation_results:
                return ""
                
            annotation_result = result.annotation_results[0]
            if annotation_result.speech_transcriptions:
                transcript_pieces = []
                for transcription in annotation_result.speech_transcriptions:
                    if transcription.alternatives:
                        best_alternative = transcription.alternatives[0]
                        transcript_pieces.append(best_alternative.transcript)
                return " ".join(transcript_pieces)
            return ""
        except Exception as e:
            logger.error(f"Error during audio transcription: {e}")
            raise Exception(f"Failed to transcribe audio using Video Intelligence API: {e}")
        finally:
            if os.path.exists(video_filename):
                os.remove(video_filename)
