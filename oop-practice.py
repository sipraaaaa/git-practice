# OOP_practice.py
# A supermarket that buys stock from a supplier and sells it to shoppers for a profit.


class Product:
    """A food item with a cost price, sell price and stock quantity."""
    def __init__(self, name, cost, price, quantity=0):
        self.name = name
        self.cost = cost          # what the supermarket pays the supplier
        self.price = price        # what the shopper pays
        self.quantity = quantity  # how many are in stock


class Department:
    """A section of the shop, e.g. Frozen Foods."""
    def __init__(self, name):
        self.name = name
        self.products = {}        # product name -> Product


# Department types inherit everything from Department
class Frozen(Department):
    def __init__(self):
        super().__init__("Frozen Foods")

class FreshProduce(Department):
    def __init__(self):
        super().__init__("Fresh Fruit and Veg")

class Tinned(Department):
    def __init__(self):
        super().__init__("Tinned Goods")

class Pharmacy(Department):
    def __init__(self):
        super().__init__("Pharmaceuticals")


class Supplier:
    """Sells products to the supermarket."""
    def supply(self, name, cost, price, quantity):
        return Product(name, cost, price, quantity)


class Shopper:
    """A customer with a budget."""
    def __init__(self, name, budget):
        self.name = name
        self.budget = budget


class Supermarket:
    """Buys stock, sells to shoppers and tracks profit."""
    def __init__(self):
        self.departments = {}
        self.spent = 0     # money paid to suppliers
        self.earned = 0    # money received from shoppers

    def add_department(self, dept):
        self.departments[dept.name] = dept

    def buy(self, supplier, dept_name, name, cost, price, quantity):
        """Buy stock from a supplier and put it in a department."""
        product = supplier.supply(name, cost, price, quantity)
        dept = self.departments[dept_name]
        if name in dept.products:
            dept.products[name].quantity += quantity   # already stocked: add more
        else:
            dept.products[name] = product
        self.spent += cost * quantity

    def sell(self, shopper, dept_name, name, quantity):
        """Sell to a shopper if there is enough stock and they can afford it."""
        product = self.departments[dept_name].products.get(name)
        if product is None or product.quantity < quantity:
            print("Not enough stock of", name)
        elif shopper.budget < product.price * quantity:
            print(shopper.name, "can't afford", name)
        else:
            product.quantity -= quantity
            shopper.budget -= product.price * quantity
            self.earned += product.price * quantity
            print(f"{shopper.name} bought {quantity} x {name}")

    def profit(self):
        """Profit = money earned - money spent."""
        return self.earned - self.spent


# Example run
shop = Supermarket()
for d in (Frozen(), FreshProduce(), Tinned(), Pharmacy()):
    shop.add_department(d)

supplier = Supplier()
shop.buy(supplier, "Frozen Foods", "Peas", 0.50, 1.00, 20)
shop.buy(supplier, "Fresh Fruit and Veg", "Apples", 0.20, 0.50, 50)
shop.buy(supplier, "Tinned Goods", "Beans", 0.40, 0.90, 30)
shop.buy(supplier, "Pharmaceuticals", "Paracetamol", 1.00, 2.50, 10)
print(f"Profit after buying stock: £{shop.profit():.2f}")

alice = Shopper("Alice", 10)
shop.sell(alice, "Fresh Fruit and Veg", "Apples", 6)
shop.sell(alice, "Pharmaceuticals", "Paracetamol", 2)
print(f"Profit now: £{shop.profit():.2f}")