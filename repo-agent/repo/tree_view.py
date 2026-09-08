def print_tree(tree, indent=""):
    for key, value in tree.items():
        print(indent + key)
        print_tree(value, indent + "  ")