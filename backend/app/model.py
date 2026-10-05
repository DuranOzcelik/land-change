"""ResNet18 land-cover classifier (fine-tuned on EuroSAT RGB)."""
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torchvision import models

CLASSES = [
    "AnnualCrop", "Forest", "HerbaceousVegetation", "Highway", "Industrial",
    "Pasture", "PermanentCrop", "Residential", "River", "SeaLake",
]
WEIGHTS = Path(__file__).resolve().parent.parent / "weights" / "resnet18.pt"

torch.set_num_threads(2)


class Classifier:
    """Wraps the network so the rest of the app only deals with numpy arrays."""

    def __init__(self, weights: Path = WEIGHTS):
        net = models.resnet18(weights=None)
        net.fc = nn.Linear(net.fc.in_features, len(CLASSES))
        net.load_state_dict(torch.load(weights, map_location="cpu"))
        self.net = net.eval()

    @torch.no_grad()
    def __call__(self, tiles: np.ndarray) -> np.ndarray:
        """tiles: float32 array (N, 3, 64, 64), already normalised. Returns (N, 10) probabilities."""
        logits = self.net(torch.from_numpy(tiles))
        return torch.softmax(logits, dim=1).numpy()
