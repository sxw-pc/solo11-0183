def ancestor_ids(animals, animal_id):
    animal = animals.get(animal_id, {})
    return [value for value in (animal.get('sire'), animal.get('dam')) if value]
