"""Bake curated VEGAN / VEGETARIAN picks into restaurants.json so diet users get INSTANT
chain-specific picks (no AI wait). Source: banked research [[chain-menu-picks-and-food-images]]
+ well-known public menus. Calories are approximate (standard build); macros intentionally OMITTED
(the research has calories, not full P/C/F — never fabricate macros for a health app).

Only chains with CONFIDENT picks are curated here; the rest (chicken-only / c-stores with pork-in-
sides or cross-contamination risk) fall back to the on-demand AI picks, which carry the
'always confirm' disclaimer. Run:  python data/add_diet_picks.py
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "restaurants.json")


def p(name, calories, why):
    return {"name": name, "calories": calories, "why": why}


# chain -> {"vegan": [...], "vegetarian": [...]}
DIET_PICKS = {
    "McDonald's": {
        "vegan": [p("Apple Slices", 15, "Crisp, sweet, basically free calories"),
                  p("Side Salad (no chicken, no dressing)", 15, "Fresh greens to start the meal")],
        "vegetarian": [p("Fruit & Maple Oatmeal (no brown sugar)", 260, "Warm, filling whole-grain breakfast"),
                       p("Hash Browns", 140, "Crispy potato fix, keep it to one"),
                       p("Egg McMuffin (no Canadian bacon)", 300, "Egg + cheese on a muffin, real protein")],
    },
    "Chick-fil-A": {
        "vegan": [p("Fruit Cup", 70, "Mixed fresh fruit, naturally sweet"),
                  p("Waffle Potato Fries", 420, "Cooked in canola — the vegan splurge")],
        "vegetarian": [p("Kale Crunch Side", 170, "Crunchy kale + almonds, bright dressing"),
                       p("Greek Yogurt Parfait", 270, "Protein + berries, light and sweet"),
                       p("Egg White Grill (no chicken)", 170, "Egg whites + cheese on a muffin")],
    },
    "Wendy's": {
        "vegan": [p("Plain Baked Potato", 270, "Whole, filling, fiber-rich — skip the butter"),
                  p("Garden Side Salad (no cheese, no dressing)", 25, "Fresh greens, almost no calories"),
                  p("Apple Bites", 35, "Sweet, crunchy, zero guilt")],
        "vegetarian": [p("Baked Potato with Cheese", 340, "Hearty, satisfying, real cheese"),
                       p("Small Frosty", 340, "The classic treat, portion-controlled")],
    },
    "Burger King": {
        "vegan": [p("Impossible Whopper (no mayo)", 630, "Plant patty, all the flame-grilled flavor"),
                  p("French Fries", 320, "Vegan fries — share or size down")],
        "vegetarian": [p("Impossible Whopper", 630, "Plant patty with the full Whopper build")],
    },
    "Taco Bell": {
        "vegan": [p("Spicy Potato Soft Taco (Fresco Style)", 230, "Flavorful potatoes, fresco swaps the dairy"),
                  p("Bean Burrito (Fresco, no cheese)", 350, "Filling beans, big flavor, plant-based"),
                  p("Black Beans & Rice", 170, "Simple, hearty, protein-packed side")],
        "vegetarian": [p("Black Bean Crunchwrap Supreme", 510, "Crunchy, cheesy, genuinely satisfying"),
                       p("Veggie Power Menu Bowl", 430, "Beans, rice, guac, greens in one bowl"),
                       p("Cheesy Bean & Rice Burrito", 420, "Cheap, warm, comforting classic")],
    },
    "Subway": {
        "vegan": [p("Veggie Delite 6\" (no cheese)", 230, "Pile on the veggies, light and fresh"),
                  p("Veggie Delite Salad", 60, "All the veggies, none of the bread")],
        "vegetarian": [p("Veggie Patty 6\"", 390, "Plant patty + veggies, real staying power"),
                       p("Veggie Delite with Cheese", 280, "Fresh veggies, a little cheese for richness")],
    },
    "Starbucks": {
        "vegan": [p("Chickpea Bites & Avocado Protein Box", 560, "Plant protein + healthy fats, very filling"),
                  p("Plain Oatmeal", 160, "Warm whole grains, add fruit not sugar")],
        "vegetarian": [p("Spinach, Feta & Egg White Wrap", 290, "20g protein, savory and light"),
                       p("Egg White & Red Pepper Egg Bites", 170, "Protein-dense, low-cal, satisfying")],
    },
    "Dunkin'": {
        "vegan": [p("Avocado Toast", 240, "Creamy avocado on sourdough, real food"),
                  p("Hash Browns", 130, "Crispy little potato bites")],
        "vegetarian": [p("Egg White Veggie Omelet Bites", 180, "13g protein, veggie-packed, low-cal"),
                       p("Veggie Egg White Wake-Up Wrap", 190, "Egg whites + veggies in a small wrap")],
    },
    "Panera Bread": {
        "vegan": [p("Ten Vegetable Soup", 150, "Brothy, veggie-loaded, genuinely light"),
                  p("Mediterranean Veggie Sandwich (no feta)", 450, "Hummus, peppers, greens — big flavor")],
        "vegetarian": [p("Mediterranean Veggie Sandwich", 530, "Hummus, feta, veggies on grain bread"),
                       p("Greek Salad", 400, "Feta, olives, crisp greens, bright dressing")],
    },
    "Chipotle": {
        "vegan": [p("Sofritas Bowl (no cheese/sour cream)", 520, "Spicy tofu, beans, fajitas, guac — flagship vegan"),
                  p("Veggie Bowl with Guac (no cheese)", 500, "Fajita veggies, beans, rice, creamy guac")],
        "vegetarian": [p("Veggie Bowl with Cheese & Guac", 570, "Add cheese for a richer veggie bowl"),
                       p("Sofritas Bowl with Cheese", 580, "Spicy tofu bowl with the dairy left in")],
    },
    "Panda Express": {
        "vegan": [p("Eggplant Tofu", 340, "Sweet-savory tofu + eggplant, real flavor"),
                  p("Super Greens", 130, "Broccoli, kale, cabbage — light and crisp")],
        "vegetarian": [p("Eggplant Tofu", 340, "Sweet-savory tofu + eggplant, real flavor"),
                       p("Veggie Spring Rolls", 240, "Crispy, shareable veggie starter")],
    },
    "Five Guys": {
        "vegan": [p("Veggie Sandwich (lettuce wrap, no cheese/mayo)", 280, "Grilled veggies stacked, fresh and light"),
                  p("Little Fries", 530, "Peanut-oil fries — vegan, share them")],
        "vegetarian": [p("Veggie Sandwich with Cheese", 440, "Grilled veggies + melty cheese on a bun"),
                       p("Grilled Cheese", 470, "Simple, gooey, hits the spot")],
    },
    "Jersey Mike's": {
        "vegan": [p("The Veggie in a Tub (no cheese)", 150, "All the veggies Mike's Way, no bread")],
        "vegetarian": [p("The Veggie Sub", 600, "Provolone + Swiss + veggies, hearty sub"),
                       p("Grilled Veggie Sub", 450, "Warm grilled peppers, onions, mushrooms")],
    },
    "Sweetgreen": {
        "vegan": [p("Shroomami Bowl", 665, "Roasted mushrooms, tofu, warm rice bowl"),
                  p("Curry Chickpea Bowl", 560, "Spiced chickpeas, greens, big plant protein")],
        "vegetarian": [p("Kale Caesar (no chicken)", 400, "Crunchy kale, parm, light caesar"),
                       p("Harvest Bowl (no chicken)", 550, "Sweet potato, apple, goat cheese, wild rice")],
    },
    "CAVA": {
        "vegan": [p("Falafel Crunch Bowl", 500, "Crispy falafel, greens, hummus, tahini"),
                  p("Harissa Avocado + Falafel Bowl", 540, "Spicy, creamy, plant-protein loaded")],
        "vegetarian": [p("Greek Salad Bowl", 585, "Feta, olives, crisp veg, lemon dressing"),
                       p("Falafel + Tzatziki Bowl", 520, "Falafel with cool tzatziki and greens")],
    },
    "Qdoba": {
        "vegan": [p("Grilled Veggie Bowl (no cheese/crema)", 450, "Fajita veggies, beans, rice, free guac"),
                  p("Impossible Taco Salad (no cheese)", 480, "Plant 'meat', greens, salsa — filling")],
        "vegetarian": [p("Impossible Taco Salad", 550, "Plant 'meat' with cheese and crema"),
                       p("Veggie Quesadilla", 520, "Melty, warm, classic comfort")],
    },
    "Shake Shack": {
        "vegan": [p("Veggie Shack (no cheese)", 400, "Veggie patty stacked with fresh toppings"),
                  p("Fries", 470, "Crinkle-cut and vegan — share them")],
        "vegetarian": [p("'Shroom Burger", 530, "Crispy cheese-stuffed portobello — iconic"),
                       p("Veggie Shack", 430, "Veggie patty with cheese and ShackSauce")],
    },
    "Wawa": {
        "vegan": [p("Roasted Veggie Quinoa Bowl + Hummus", 400, "Warm grains, roasted veg, creamy hummus"),
                  p("Garden Salad (no cheese)", 150, "Fresh greens base, add oil & vinegar")],
        "vegetarian": [p("Egg White Omelet Burrito Bowl", 180, "Light, high-protein, veggie-friendly"),
                       p("Veggie Hoagie with Cheese", 420, "Build it your way, provolone + veggies"),
                       p("Avocado Toast", 320, "Creamy avocado on toasted bread")],
    },
    "Sheetz": {
        "vegan": [p("MTO Veggie Bowl (grains + roasted veg + hummus)", 400, "Build-your-own warm veggie bowl"),
                  p("Veggie Sub (no cheese)", 350, "Load the veggies, skip the dairy")],
        "vegetarian": [p("Grilled Cheese", 430, "Crispy, gooey, comfort classic"),
                       p("Veggie Sub with Provolone", 430, "Fresh veggies + melty cheese"),
                       p("Egg & Cheese on a Pretzel Roll", 380, "Warm, savory, protein to start the day")],
    },
    "7-Eleven": {
        "vegan": [p("Hummus & Veggie Snack Pack", 250, "Crunchy veggies + protein-rich hummus"),
                  p("Fresh Fruit Cup", 90, "Pre-cut fruit, grab-and-go sweet")],
        "vegetarian": [p("Garden Salad with Cheese", 200, "Fresh greens, a little cheese, light dressing"),
                       p("Greek Yogurt Parfait", 230, "Protein + granola + berries")],
    },
}


def main():
    with open(PATH, encoding="utf-8") as f:
        data = json.load(f)
    added, missing = [], []
    have = {r.get("chain") for r in data["restaurants"]}
    for chain in DIET_PICKS:
        if chain not in have:
            missing.append(chain)
    for r in data["restaurants"]:
        dp = DIET_PICKS.get(r.get("chain"))
        if dp:
            r["diet_picks"] = dp
            added.append(r["chain"])
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"diet_picks added to {len(added)} chains: {', '.join(added)}")
    if missing:
        print(f"WARNING — curated but not in restaurants.json (name mismatch?): {', '.join(missing)}")
    ai = [r["chain"] for r in data["restaurants"] if "diet_picks" not in r]
    print(f"\n{len(ai)} chains use the on-demand AI diet fallback: {', '.join(ai)}")


if __name__ == "__main__":
    main()
