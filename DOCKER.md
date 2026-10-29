# Akubot in Docker Desktop

Keep the **akubot** group expanded to see its two running components.

| Docker Desktop service (container name) | Its job | What happens if you stop it |
| --- | --- | --- |
| **dashboard-and-discord** (`akubot-dashboard-and-discord`) | Runs the local web dashboard, saves tuning/personality settings, and connects to Discord. | The dashboard and Discord replies stop. |
| **local-ai-ollama** (`akubot-local-ai-ollama`) | Runs Ollama, the local engine that loads the selected AI model and generates replies using the GPU. | The dashboard can stay open, but model-generated replies are unavailable. |

The dashboard address is **http://localhost:8765**. Docker may show `8765:8787`: 8765 is the port you open in your browser; 8787 is the dashboard's internal container port.

Ollama is the model runner. Llama and SmolLM2 are model files it can load. The trained LoRA styles are installed model builds selected through the dashboard. There is no separate running container for each model or LoRA.

## Which controls do what?

| Control | What it affects |
| --- | --- |
| Dashboard: Base model and LoRA style | Which installed model build Ollama uses for the next reply. |
| Dashboard: Creativity, reply length, context window | Parameters sent to the model when asking it to generate a reply. |
| Dashboard: Keep model ready | How long the model stays loaded in GPU memory between replies. |
| Dashboard: Cooldown, server request limits, conversation memory | Behavior of the Discord bot application. |
| Dashboard: Personality | Instructions and public facts included with model requests. |
| Docker: Stop/start the dashboard-and-Discord container | Controls both the web dashboard and the bot's Discord connection. |
| Docker: Stop/start the local-AI-Ollama container | Controls the model engine. It does not serve the dashboard. |

`akubot:local` and `ollama/ollama` are image names, meaning the software packages used to create these containers. They are different from the selected AI model's name.

Docker Desktop's expanded Compose group displays service names. Those are now `dashboard-and-discord` and `local-ai-ollama`; the actual container names have the `akubot-` prefix shown above. Use `docker compose logs dashboard-and-discord` for application logs. The private network keeps the `ollama` alias so the bot and offline tools retain their existing model connection.

Training uses separate, temporary containers and the scripts in [the WSL training guide](training/README.md). Training containers are not needed for ordinary Discord replies or dashboard use.

Source code is in `~/workspace/akubot`. Tokens, settings, archive data, trained models, and model caches remain in private WSL storage outside the repository. Renaming a container does not move or delete these files.
