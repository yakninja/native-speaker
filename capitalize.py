# Path to your text file
file_path = 'top-100-1000.txt'
# Path for the modified file (can be the same as file_path if you want to overwrite)
modified_file_path = 'top-100-1000-capitalized.txt'

# Read the contents of the file
with open(file_path, 'r') as file:
    lines = file.readlines()

# Capitalize the first character of each line
modified_lines = [line.capitalize() + "\n" for line in lines]

# Write the modified lines back to a file
with open(modified_file_path, 'w') as modified_file:
    modified_file.writelines(modified_lines)
