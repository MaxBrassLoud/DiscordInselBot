import math

def calculate_level(out):
    a = 49.6
    b = 131.6
    c = 1.8 - out
    
    discriminant = b**2 - 4*a*c
    if discriminant < 0:
        return None  # kein reelles Ergebnis
    
    x1 = (-b + math.sqrt(discriminant)) / (2*a)

    
    return round(x1)

