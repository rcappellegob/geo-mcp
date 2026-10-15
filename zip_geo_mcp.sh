set -euo

SOURCE_DIR="/srv/geo-mcp"
OUTPUT_FILE="/tmp/geo-mcp.zip"

# Remove existing archive if present
rm -f "${OUTPUT_FILE}"

# Verify source exists
if [ ! -d "${SOURCE_DIR}" ]; then
    echo "ERROR: Directory ${SOURCE_DIR} does not exist."
    exit 1
fi

echo "Creating archive: ${OUTPUT_FILE}"

# Create ZIP archive
cd "$(dirname "${SOURCE_DIR}")"
zip -r "${OUTPUT_FILE}" "$(basename "${SOURCE_DIR}")" \
    -x "*/.git/*" \
    -x "*/__pycache__/*" \
    -x "*.pyc"

echo "Archive created successfully:"
ls -lh "${OUTPUT_FILE}"
