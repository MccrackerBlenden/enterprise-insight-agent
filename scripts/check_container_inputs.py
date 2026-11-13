#!/usr/bin/env python3
"""Verify Docker/Vite inputs are actually present in the distributable package."""
from pathlib import Path
import json
root=Path(__file__).resolve().parents[1]
required=['backend/Dockerfile','frontend/Dockerfile','frontend/nginx.conf','frontend/public/eia-brand-icon.png','frontend/public/favicon.png','frontend/public/release.json','compose.yaml']
for item in required:
    assert (root/item).is_file(),f'MISSING Docker input: {item}'
    print('PASS:',item)
fd=(root/'frontend/Dockerfile').read_text()
assert 'COPY public ./public' in fd,'Frontend Docker image does not include brand assets in Vite build'
assert 'COPY --from=frontend_build /webapp/dist' in fd,'Frontend Docker image does not publish Vite build'
assert 'COPY nginx.conf' in fd,'Nginx configuration is absent in final image'
ct=(root/'compose.yaml').read_text()
assert '${EIA_WEB_PORT:-8080}' in ct,'Isolated port override missing'
manifest=json.loads((root/'frontend/public/release.json').read_text())
assert manifest['release_id']=='eia-portfolio-20261113'
print('PASS: Docker inputs and release marker consistent')
