"""
Smart Upload Parser - Intelligently extracts entities from uploaded lab reports
No regex filename parsing - extracts patient name, test type, and analytes from content
"""
import os
import re
import json
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
load_dotenv(ROOT / ".env")

PARSE_DIR = ROOT / "data" / "parsed"
PARSE_DIR.mkdir(parents=True, exist_ok=True)

# OpenAI client for entity extraction
openai_client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY") or os.getenv("LLM_BINDING_API_KEY")
)
MODEL_NAME = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"


class SmartUploadParser:
    """
    Intelligently parses uploaded lab reports and extracts entities
    """
    
    def __init__(self):
        self.openai_client = openai_client
        self.model_name = MODEL_NAME
    
    def parse_file_with_docling(self, file_path: Path, output_dir: Path) -> Tuple[str, List[Dict]]:
        """
        Parse file using appropriate method:
        - PDFs: Docling CLI
        - Images: OpenAI Vision API
        
        Returns:
            Tuple of (markdown_content, content_list)
        """
        file_ext = file_path.suffix.lower()
        image_extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp', '.tiff', '.tif'}
        
        # For images, use Vision API directly (Docling doesn't support images)
        if file_ext in image_extensions:
            print(f"   Detected image file, using OpenAI Vision API...")
            markdown_content = self.parse_image_with_vision(file_path)
            return markdown_content, []
        
        # For PDFs and other documents, use Docling
        try:
            # Run Docling for PDFs and Office docs
            result = subprocess.run(
                ["docling", str(file_path), "--output", str(output_dir), "--export-type", "md"],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                raise Exception(f"Docling failed: {result.stderr}")
            
            # Read markdown output
            md_file = output_dir / f"{file_path.stem}.md"
            if md_file.exists():
                markdown_content = md_file.read_text(encoding="utf-8")
            else:
                markdown_content = ""
            
            # Read JSON output (if available)
            json_file = output_dir / f"{file_path.stem}.json"
            content_list = []
            if json_file.exists():
                with open(json_file, 'r', encoding='utf-8') as f:
                    content_list = json.load(f)
            
            return markdown_content, content_list
            
        except subprocess.TimeoutExpired:
            print(f"⚠️  Docling parsing timed out after 120 seconds")
            return self.fallback_extraction(file_path), []
        except Exception as e:
            print(f"⚠️  Docling parsing failed: {e}")
            return self.fallback_extraction(file_path), []
    
    def parse_image_with_vision(self, image_path: Path) -> str:
        """
        Parse image using OpenAI Vision API
        Perfect for lab reports - extracts all text accurately
        """
        try:
            import base64
            
            # Read and encode image
            with open(image_path, 'rb') as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')
            
            # Determine image type
            ext = image_path.suffix.lower()
            mime_types = {
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.bmp': 'image/bmp',
                '.gif': 'image/gif',
                '.webp': 'image/webp'
            }
            mime_type = mime_types.get(ext, 'image/jpeg')
            
            # Call Vision API
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",  # Vision-capable model
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": """Extract ALL text from this medical lab report image. 
                                
Preserve:
- All test names and values
- Reference ranges
- Patient information
- Dates
- Units
- Table structure

Return the extracted text in a clear, structured format."""
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_data}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=4096
            )
            
            extracted_text = response.choices[0].message.content
            return extracted_text
            
        except Exception as e:
            print(f"⚠️  Vision API extraction failed: {e}")
            return ""
    
    def fallback_extraction(self, file_path: Path) -> str:
        """Fallback extraction based on file type"""
        if file_path.suffix.lower() == '.pdf':
            return self.fallback_pdf_extraction(file_path)
        else:
            # For images, use Vision API
            print(f"   Using OpenAI Vision API for image extraction...")
            return self.parse_image_with_vision(file_path)
    
    def fallback_pdf_extraction(self, pdf_path: Path) -> str:
        """Fallback PDF text extraction using pypdf"""
        try:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            return text
        except:
            return ""
    
    def extract_entities_with_llm(self, content: str) -> Dict:
        """
        Extract entities from lab report content using LLM
        
        Returns:
            {
                "patient_name": "John Smith",
                "patient_id": "P100001" or None,
                "test_type": "RFT",
                "test_date": "2023-05-12",
                "analytes": [
                    {"name": "Creatinine", "value": "1.06", "unit": "mg/dL", "range": "0.74-1.35"},
                    ...
                ]
            }
        """
        system_prompt = """You are a medical lab report parser. Extract structured information from the report.

Extract:
1. Patient Name (full name)
2. Patient ID (if present, usually starts with P followed by numbers)
3. Test Type (e.g., RFT, LFT, CBC, Thyroid, Lipid_Profile, CMP, Diabetes, etc.)
4. Test Date (in YYYY-MM-DD format)
5. Analytes with their values, units, and reference ranges

Return ONLY valid JSON in this exact format:
{
  "patient_name": "First Last",
  "patient_id": "P123456" or null,
  "test_type": "RFT",
  "test_date": "2023-05-12",
  "analytes": [
    {"name": "Creatinine", "value": "1.06", "unit": "mg/dL", "range": "0.74-1.35", "status": "normal"},
    {"name": "BUN", "value": "15", "unit": "mg/dL", "range": "7-20", "status": "normal"}
  ]
}

Test type mappings:
- Kidney/Renal tests → RFT
- Liver tests → LFT
- Blood count → CBC
- Thyroid (T3, T4, TSH) → Thyroid
- Cholesterol, Lipids → Lipid_Profile
- Comprehensive Metabolic Panel → CMP
- Glucose, HbA1c → Diabetes
- CRP, ESR → Inflammation
- Ferritin, Iron → Iron_Studies
- Vitamin D, B12 → Vitamin

If information is missing, use null for that field."""

        try:
            response = self.openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Extract entities from this lab report:\n\n{content[:4000]}"}
                ],
                temperature=0,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            
            # Validate and clean
            if not result.get("patient_name"):
                result["patient_name"] = "Unknown"
            if not result.get("test_type"):
                result["test_type"] = "General"
            if not result.get("test_date"):
                result["test_date"] = datetime.now().strftime("%Y-%m-%d")
            if not result.get("analytes"):
                result["analytes"] = []
            
            return result
            
        except Exception as e:
            print(f"⚠️  Entity extraction failed: {e}")
            return {
                "patient_name": "Unknown",
                "patient_id": None,
                "test_type": "General",
                "test_date": datetime.now().strftime("%Y-%m-%d"),
                "analytes": []
            }
    
    def generate_patient_id(self, patient_name: str) -> str:
        """
        Generate a patient ID from name
        Format: P + hash of name (6 digits)
        """
        import hashlib
        name_hash = hashlib.md5(patient_name.lower().encode()).hexdigest()[:6]
        return f"P{name_hash.upper()}"
    
    def generate_filename(self, entities: Dict) -> str:
        """
        Generate filename based on extracted entities
        Format: {patient_id}_{test_type}_{date}.md
        """
        patient_id = entities.get("patient_id")
        if not patient_id:
            # Generate ID from name
            patient_id = self.generate_patient_id(entities["patient_name"])
            # Persist it back into entities so downstream consumers (markdown, cache, chroma metadata)
            # have a stable patient_id even when the report didn't include one.
            entities["patient_id"] = patient_id
        
        test_type = entities.get("test_type", "General").replace(" ", "_")
        test_date = entities.get("test_date", datetime.now().strftime("%Y-%m-%d"))
        date_str = test_date.replace("-", "")  # 2023-05-12 → 20230512
        
        return f"{patient_id}_{test_type}_{date_str}.md"
    
    def format_markdown_output(self, entities: Dict, raw_markdown: str) -> str:
        """
        Format the final markdown output with extracted entities
        """
        output = f"""# Lab Report - {entities['test_type']}

**Patient:** {entities['patient_name']}  
**Patient ID:** {entities.get('patient_id') or 'Not specified'}  
**Test Date:** {entities['test_date']}  
**Test Type:** {entities['test_type']}

---

## Test Results

"""
        
        # Add analytes table if available
        if entities.get('analytes'):
            output += "| Analyte | Value | Unit | Reference Range | Status |\n"
            output += "|---------|-------|------|----------------|--------|\n"
            
            for analyte in entities['analytes']:
                name = analyte.get('name', 'Unknown')
                value = analyte.get('value', '-')
                unit = analyte.get('unit', '')
                range_val = analyte.get('range', '-')
                status = analyte.get('status', 'unknown')
                output += f"| {name} | {value} | {unit} | {range_val} | {status} |\n"
            
            output += "\n---\n\n"
        
        # Append original content
        output += "## Original Report Content\n\n"
        output += raw_markdown
        
        return output
    
    def process_upload(
        self,
        file_path: Path,
        temp_output_dir: Path
    ) -> Dict:
        """
        Main processing pipeline:
        1. Parse file (PDF/image)
        2. Extract entities with LLM
        3. Generate proper filename
        4. Save to data/parsed
        5. Return extracted entities
        
        Returns:
            {
                "success": True,
                "entities": {...},
                "markdown_path": Path,
                "markdown_content": str
            }
        """
        print(f"\n📄 Processing upload: {file_path.name}")
        
        try:
            # Step 1: Parse file (PDF or image)
            print(f"  [1/4] Parsing {file_path.suffix} file with Docling...")
            markdown_content, content_list = self.parse_file_with_docling(file_path, temp_output_dir)
            
            if not markdown_content:
                return {
                    "success": False,
                    "error": "Failed to extract content from file"
                }
            
            # Step 2: Extract entities
            print("  [2/4] Extracting entities with LLM...")
            entities = self.extract_entities_with_llm(markdown_content)
            
            print(f"    ✓ Patient: {entities['patient_name']}")
            print(f"    ✓ Test Type: {entities['test_type']}")
            print(f"    ✓ Date: {entities['test_date']}")
            print(f"    ✓ Analytes: {len(entities.get('analytes', []))}")
            
            # Step 3: Generate filename and format content
            print("  [3/4] Generating structured output...")
            filename = self.generate_filename(entities)
            formatted_content = self.format_markdown_output(entities, markdown_content)
            
            # Step 4: Save to data/parsed
            print("  [4/4] Saving to data/parsed...")
            output_path = PARSE_DIR / filename
            output_path.write_text(formatted_content, encoding="utf-8")
            
            print(f"  ✅ Saved as: {filename}")
            
            return {
                "success": True,
                "entities": entities,
                "markdown_path": output_path,
                "markdown_content": formatted_content,
                "filename": filename
            }
            
        except Exception as e:
            print(f"  ❌ Processing failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }


# Singleton instance
_parser = None

def get_parser() -> SmartUploadParser:
    """Get or create parser instance"""
    global _parser
    if _parser is None:
        _parser = SmartUploadParser()
    return _parser


if __name__ == "__main__":
    # Test the parser
    import sys
    if len(sys.argv) > 1:
        test_file = Path(sys.argv[1])
        if test_file.exists():
            parser = SmartUploadParser()
            temp_dir = Path("temp_parse")
            temp_dir.mkdir(exist_ok=True)
            
            result = parser.process_upload(test_file, temp_dir)
            
            if result["success"]:
                print("\n✅ SUCCESS!")
                print(json.dumps(result["entities"], indent=2))
            else:
                print(f"\n❌ FAILED: {result['error']}")
        else:
            print(f"File not found: {test_file}")
    else:
        print("Usage: python smart_upload_parser.py <pdf_file>")
