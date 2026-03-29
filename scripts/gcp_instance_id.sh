#!/bin/bash

# Get container instance ID from Google Cloud metadata or generate a random UUID
get_container_instance_id() {
    # Ref: https://cloud.google.com/run/docs/container-contract#metadata-server
    local instance_id=$(curl -s -f -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/id 2>/dev/null)
    
    if [ $? -eq 0 ] && [ -n "$instance_id" ]; then
        echo "$instance_id"
    else
        # If the metadata server is not available, generate a random UUID.
        local instance_id=$(python -c "import uuid; print(uuid.uuid4())")
        echo "$instance_id"
    fi
}