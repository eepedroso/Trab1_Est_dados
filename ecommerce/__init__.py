"""Arquivos sequenciais indexados de produtos e acessos de e-commerce."""

from .models import Access, Product
from .repository import EcommerceRepository

__all__ = ["Access", "Product", "EcommerceRepository"]
