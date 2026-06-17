"""
Batch generation script for GTA5 Mission Pipeline.

Usage:
  python batch_generate.py prompts.txt
"""

from pathlib import Path
import typer
import yaml
from rich.console import Console

from src.pipeline import MissionPipeline

app = typer.Typer(help="Batch generate multiple GTA5 missions.")
console = Console()

@app.command()
def batch(
    prompts_file: str = typer.Argument(..., help="Text file containing one mission prompt per line"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """Generate multiple missions from a text file (one prompt per line)."""
    path = Path(prompts_file)
    if not path.exists():
        console.print(f"[red]Error:[/] Prompts file '{prompts_file}' not found.")
        raise typer.Exit(code=1)
        
    # Read prompts, ignoring empty lines and comments
    prompts = [
        line.strip() 
        for line in path.read_text(encoding="utf-8").splitlines() 
        if line.strip() and not line.strip().startswith("#")
    ]
    
    if not prompts:
        console.print(f"[yellow]Warning:[/] No prompts found in '{prompts_file}'.")
        raise typer.Exit(code=0)
        
    console.print(f"[bold green]Initializing pipeline...[/]")
    try:
        with open(config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        pipeline = MissionPipeline(cfg)
    except Exception as e:
        console.print(f"[bold red]Failed to initialize pipeline:[/] {e}")
        raise typer.Exit(code=1)
    
    console.print(f"[bold green]Starting batch generation for {len(prompts)} mission(s)...[/]")
    
    successful = 0
    for i, prompt in enumerate(prompts, 1):
        console.print(f"\n[bold magenta]=== Mission {i}/{len(prompts)} ===[/]")
        console.print(f"[cyan]Prompt:[/] {prompt}")
        try:
            output_path = pipeline.run(prompt)
            successful += 1
            console.print(f"[green]Successfully generated:[/] {output_path}")
        except Exception as e:
            console.print(f"[bold red]Error generating mission:[/] {e}")
            
    console.print(f"\n[bold green]Batch complete![/] ({successful}/{len(prompts)} successful)")

if __name__ == "__main__":
    app()
